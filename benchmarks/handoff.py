"""Two MCP SDK sessions over the shipped HTTP service and stdio relay."""

import hashlib
import math
import os
import secrets
import socket
import statistics
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ListToolsResult

from benchmarks.fixture import ANCHOR, ANSWER, RATIONALE, TASK, decision, search
from mimir.budget import canonical

_RSS_LOCK = Lock()


def latency(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000, 3),
    }


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def grants(path: Path, project: str) -> tuple[str, str, str]:
    writer, reader, admin = (secrets.token_urlsafe(32) for _ in range(3))

    def principal(name: str, token: str, grant: str) -> str:
        digest = hashlib.sha256(token.encode()).hexdigest()
        return f'[principals.{name}]\ntoken_sha256="{digest}"\n{grant}\n'

    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        file.write(principal("writer", writer, f'write=["{project}"]'))
        file.write(principal("reader", reader, f'read=["{project}"]'))
        file.write(principal("probe", admin, "admin=true"))
    return writer, reader, admin


@asynccontextmanager
async def service(
    root: Path,
    data_dir: Path,
    credentials: Path,
) -> AsyncIterator[tuple[str, int, subprocess.Popen[bytes], float]]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    started = perf_counter()
    # Use the shipped CLI, with credentials scoped to the disposable store.
    process = subprocess.Popen(  # nosec B603
        [
            sys.executable,
            "-m",
            "mimir.service",
            "--data-dir",
            str(data_dir),
            "--credentials",
            str(credentials),
            "--port",
            str(port),
        ],
        cwd=root,
        env={"PATH": os.defpath, "HOME": str(root)},
        stdin=subprocess.DEVNULL,
    )
    try:
        yield base, port, process, started
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def wait_for_health(base: str, process: subprocess.Popen[bytes], admin: str) -> None:
    with anyio.fail_after(10):
        async with httpx.AsyncClient(trust_env=False, timeout=1) as http:
            while process.poll() is None:
                try:
                    result = await http.get(f"{base}/health", headers=bearer(admin))
                    if result.status_code == 200:
                        assert result.json()["semantic"] == "disabled"
                        return
                except httpx.TransportError:
                    pass
                await anyio.sleep(0.01)
    raise RuntimeError(f"Mimir service exited with status {process.returncode}")


def rss(service_pid: int) -> dict[str, int]:
    driver = os.getpid()
    # Only PID relationships and RSS are needed. Never request process arguments.
    with (
        _RSS_LOCK,
        subprocess.Popen(  # nosec B603
            ["/bin/ps", "-axo", "pid=,ppid=,rss="], stdout=subprocess.PIPE, text=True
        ) as ps,
    ):
        output, _ = ps.communicate(timeout=3)
        if ps.returncode != 0:
            raise RuntimeError("Cannot sample benchmark process RSS")
        rows = [tuple(map(int, line.split())) for line in output.splitlines()]
        owned = {pid: kib for pid, _, kib in rows}
        relays = [
            kib
            for pid, parent, kib in rows
            if parent == driver and pid not in {service_pid, ps.pid}
        ]
        assert len(relays) <= 1, "Unexpected child process during RSS sampling"
    return {
        "service_kib": owned.get(service_pid, 0),
        "relay_kib": sum(relays),
        "driver_kib": owned[driver],
    }


def success(result: CallToolResult) -> dict[str, Any]:
    envelope = result.structuredContent
    assert not result.isError and envelope and envelope["ok"], result
    data: dict[str, Any] = envelope["data"]
    return data


def call_bytes(name: str, request: dict[str, Any], result: CallToolResult) -> dict[str, int]:
    call = {"name": name, "arguments": request}
    response = result.model_dump(by_alias=True, exclude_none=True, mode="json")
    call_body = len(canonical(call))
    response_body = len(canonical(response))
    rpc_request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}
    rpc_response = {"jsonrpc": "2.0", "id": 1, "result": response}
    return {
        "tool_call_bytes": call_body,
        "tool_result_bytes": response_body,
        "estimated_rpc_framing_bytes": (
            len(canonical(rpc_request)) + len(canonical(rpc_response)) - call_body - response_body
        ),
    }


def catalog_bytes(result: ListToolsResult) -> tuple[int, int]:
    model = result.model_dump(by_alias=True, exclude_none=True, mode="json")
    body = len(canonical(model["tools"]))
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = {"jsonrpc": "2.0", "id": 1, "result": model}
    return body, len(canonical(request)) + len(canonical(response)) - body


def overhead(
    writer_catalog: ListToolsResult,
    reader_catalog: ListToolsResult,
    capture: dict[str, int],
    first_read: dict[str, int],
    repeat_reads: list[dict[str, int]],
) -> dict[str, Any]:
    task, answer = len(TASK.encode()), len(ANSWER.encode())
    manual = {
        "task_bytes": task,
        "scripted_answer_bytes": answer,
        "handoff_note_bytes": answer,
        "context_bytes": task + answer + answer,
    }
    writer_tools, writer_framing = catalog_bytes(writer_catalog)
    reader_tools, reader_framing = catalog_bytes(reader_catalog)

    def mcp(
        reads: list[dict[str, int]],
        *,
        first: bool,
    ) -> dict[str, int]:
        count = len(reads)
        totals = {
            "task_bytes": task * count,
            "scripted_answer_bytes": answer * count,
            "tool_catalog_bytes": writer_tools + reader_tools if first else 0,
            "structured_capture_call_bytes": capture["tool_call_bytes"] if first else 0,
            "capture_result_bytes": capture["tool_result_bytes"] if first else 0,
            "tool_call_bytes": sum(read["tool_call_bytes"] for read in reads),
            "tool_result_bytes": sum(read["tool_result_bytes"] for read in reads),
        }
        framing = sum(read["estimated_rpc_framing_bytes"] for read in reads)
        if first:
            framing += writer_framing + reader_framing + capture["estimated_rpc_framing_bytes"]
        return {
            **totals,
            "context_bytes": sum(totals.values()),
            "estimated_rpc_framing_bytes": framing,
            "context_plus_estimated_rpc_framing_bytes": sum(totals.values()) + framing,
        }

    first, repeat = mcp([first_read], first=True), mcp(repeat_reads, first=False)
    manual_repeat = {key: value * len(repeat_reads) for key, value in manual.items()}
    groups = {
        "first_manual": manual,
        "first_mcp": first,
        "repeat_manual": manual_repeat,
        "repeat_mcp": repeat,
    }
    return {
        **{
            phase: {**counts, "estimated_context_tokens": math.ceil(counts["context_bytes"] / 4)}
            for phase, counts in groups.items()
        },
        "repeat_turns": len(repeat_reads),
        "total_manual_context_bytes": manual["context_bytes"] + manual_repeat["context_bytes"],
        "total_mcp_context_bytes": first["context_bytes"] + repeat["context_bytes"],
        "repeat_mcp_context_bytes_if_reader_catalog_is_resent_each_turn": (
            repeat["context_bytes"] + reader_tools * len(repeat_reads)
        ),
        "unit": "UTF-8 bytes; canonical JSON for SDK objects",
        "token_estimate_method": "Round up UTF-8 context bytes divided by four; no model tokenizer",
        "measured_model_tokens": None,
    }


async def run_handoff(
    root: Path,
    data_dir: Path,
    writer_project: str,
    reader_project: str,
    unrelated: str,
    repeats: int,
) -> dict[str, Any]:
    credentials = root / "grants.toml"
    writer, reader, admin = grants(credentials, writer_project)
    proposal = decision(
        writer_project,
        "storage",
        ANSWER,
        choice="SQLite WAL",
        rationale=RATIONALE,
        anchor=ANCHOR,
    )
    query = search(reader_project, "SQLite WAL local ledger", limit=1, budget=1800)

    async with service(root, data_dir, credentials) as (base, port, process, started):
        pid = process.pid
        samples: list[dict[str, int]] = []
        finished = anyio.Event()

        async def sample_rss() -> None:
            while not finished.is_set():
                samples.append(await anyio.to_thread.run_sync(rss, pid))
                with anyio.move_on_after(0.05):
                    await finished.wait()

        async with anyio.create_task_group() as group:
            group.start_soon(sample_rss)
            try:
                await wait_for_health(base, process, admin)
                startup_ms = (perf_counter() - started) * 1000
                service_idle = rss(pid)
                samples.append(service_idle)
                with anyio.fail_after(30):
                    result, connected_idle = await exercise(
                        base,
                        port,
                        pid,
                        writer,
                        reader,
                        proposal,
                        query,
                        unrelated,
                        repeats,
                        root / "worktree",
                    )
                samples.append(connected_idle)
            finally:
                finished.set()
        result["latency"]["service_start_to_health_ms"] = round(startup_ms, 3)
        result["ram"] = {
            "service_idle": service_idle,
            "connected_idle": connected_idle,
            "peak_sampled_service_kib": max(sample["service_kib"] for sample in samples),
            "peak_sampled_relay_kib": max(sample["relay_kib"] for sample in samples),
            "peak_sampled_driver_kib": max(sample["driver_kib"] for sample in samples),
            "peak_sampled_total_kib": max(
                sample["service_kib"] + sample["relay_kib"] for sample in samples
            ),
            "samples": len(samples),
            "sample_delay_ms": 50,
        }
        return result


async def exercise(
    base: str,
    port: int,
    pid: int,
    writer: str,
    reader: str,
    proposal: dict[str, Any],
    query: dict[str, Any],
    unrelated: str,
    repeats: int,
    worktree: Path,
) -> tuple[dict[str, Any], dict[str, int]]:
    async with (
        httpx.AsyncClient(headers=bearer(writer), trust_env=False) as http,
        streamable_http_client(f"{base}/mcp", http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as a,
    ):
        await a.initialize()
        writer_catalog = await a.list_tools()
        captured = await a.call_tool("record_memory", proposal)
        written = success(captured)
        assert written["outcome"] == "added"

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mimir.stdio", "--port", str(port)],
            cwd=worktree,
            env={"MIMIR_TOKEN": reader, "HOME": str(worktree.parent)},
        )
        async with stdio_client(params) as relay, ClientSession(*relay) as b:
            await b.initialize()
            reader_catalog = await b.list_tools()
            assert len(writer_catalog.tools) == len(reader_catalog.tools) == 5
            connected_idle = rss(pid)
            assert connected_idle["relay_kib"] > 0, "stdio relay missing from RSS sample"

            started = perf_counter()
            first = await b.call_tool("search_memory", query)
            first_ms = (perf_counter() - started) * 1000
            recovered = success(first)
            (item,) = recovered["items"]
            assert item["statement"] == ANSWER and item["revision_id"] == written["revision_id"]
            assert item["source"]["anchor"] == ANCHOR
            assert "semantic_unavailable" in recovered["diagnostics"]

            warm, repeat_reads = [], []
            for _ in range(repeats):
                started = perf_counter()
                response = await b.call_tool("search_memory", query)
                warm.append(perf_counter() - started)
                assert success(response) == recovered
                repeat_reads.append(call_bytes("search_memory", query, response))

            wrong_scope = {**query, "scope": {**query["scope"], "project_id": unrelated}}
            denied = await b.call_tool("search_memory", wrong_scope)
            assert denied.structuredContent and not denied.structuredContent["ok"]
            assert denied.structuredContent["error"]["code"] == "scope_denied"

            conflict = success(
                await a.call_tool(
                    "record_memory",
                    decision(
                        proposal["project_id"],
                        "storage",
                        "Use PostgreSQL for the local ledger.",
                        choice="PostgreSQL",
                        expected=written["revision_id"],
                        request_key="storage-change",
                    ),
                )
            )
            assert conflict["outcome"] == "pending_review"
            withheld = success(await b.call_tool("search_memory", query))
            assert withheld["items"] == []
            (warning,) = withheld["conflicts"]
            assert (
                warning["code"] == "pending_review"
                and warning["conflict_id"] == conflict["conflict_id"]
            )

            history = success(
                await b.call_tool(
                    "get_history",
                    {
                        "project_id": query["scope"]["project_id"],
                        "memory_id": written["memory_id"],
                        "after_seq": 0,
                        "through_seq": None,
                        "limit": 10,
                        "token_budget": 4096,
                        "tokenizer": "utf8_bytes",
                    },
                )
            )
            events = history["events"]
            assert [event["event"] for event in events] == [
                "assert",
                "accept",
                "assert",
                "conflict_open",
            ]
            assert events[1]["revision_id"] == written["revision_id"]

    return {
        "handoff": {
            "clients": ["simulated SDK over Streamable HTTP", "simulated SDK over stdio"],
            "shared_worktree_project": True,
            "decision": item["statement"],
            "source_anchor": item["source"]["anchor"],
            "conflict": conflict["outcome"],
            "history": [event["event"] for event in events],
            "fallback": "semantic_unavailable",
            "unrelated_project": denied.structuredContent["error"]["code"],
            "scripted_answer": item["statement"],
        },
        "latency": {
            "first_stdio_search_ms": round(first_ms, 3),
            "warm_stdio_search": latency(warm),
        },
        "overhead": overhead(
            writer_catalog,
            reader_catalog,
            call_bytes("record_memory", proposal, captured),
            call_bytes("search_memory", query, first),
            repeat_reads,
        ),
    }, connected_idle
