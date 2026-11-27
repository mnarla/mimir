"""Synthetic Git projects and contract payloads shared by the demo and benchmark."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

START = "2026-01-01T00:00:00.000000Z"
AS_OF = "2026-06-01T00:00:00.000000Z"
DECISION = "Use SQLite WAL for the local ledger."
ANCHOR = "ADR-001: local ledger storage"
TASK = "Which storage engine and journal mode should the local ledger use, and why?"
RATIONALE = "The ledger needs crash recovery and concurrent readers on one machine."
ANSWER = f"{DECISION} {RATIONALE}"


def projects(root: Path, data_dir: Path) -> tuple[str, str, str]:
    repo, worktree, other = (root / name for name in ("repo", "worktree", "unrelated"))
    env = {
        "PATH": os.defpath,
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    def git(*args: str) -> None:
        # Only run fixed Git operations against the disposable repositories.
        subprocess.run(  # nosec B603
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            check=True,
            timeout=10,
        )

    for path in (repo, other):
        git("init", "--quiet", "--template=", str(path))
    git(
        "-C",
        str(repo),
        "worktree",
        "add",
        "--quiet",
        "--orphan",
        "-b",
        "synthetic-worktree",
        str(worktree),
    )

    def resolve(path: Path) -> str:
        result = subprocess.run(  # nosec B603
            [
                sys.executable,
                "-m",
                "brain",
                "project",
                "resolve",
                "--cwd",
                str(path),
                "--data-dir",
                str(data_dir),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            check=True,
            timeout=10,
        )
        resolved = json.loads(result.stdout)
        assert resolved["project_id"] and not resolved["needs_review"], resolved
        return str(resolved["project_id"])

    writer, reader, unrelated = resolve(repo), resolve(worktree), resolve(other)
    assert writer == reader and writer != unrelated
    return writer, reader, unrelated


def decision(
    project: str,
    key: str,
    statement: str,
    *,
    choice: str,
    rationale: str = "Synthetic benchmark decision",
    anchor: str = "Synthetic decision",
    effective: str = START,
    expires: str | None = None,
    expected: str | None = None,
    request_key: str | None = None,
) -> dict[str, Any]:
    return {
        "project_id": project,
        "idempotency_key": request_key or key,
        "identity": {"entity": "engine", "key": key, "scope": "repo"},
        "statement": statement,
        "payload": {
            "type": "Decision",
            "data": {"choice": {"kind": "string", "value": choice}, "rationale": rationale},
        },
        "cardinality": "single",
        "valid_from": effective,
        "valid_to": expires,
        "expected_revision": expected,
        "supersedes_revision": None,
        "source": {
            "source_tool": "mcp-sdk-fixture",
            "source_agent": None,
            "session_id": "synthetic-writer",
            "timestamp": START,
            "anchor": anchor,
            "locator": None,
        },
    }


def search(
    project: str, query: str, *, limit: int = 5, known: int | None = None, budget: int = 16384
) -> dict[str, Any]:
    return {
        "scope": {
            "project_id": project,
            "additional_projects": [],
            "valid_at": AS_OF,
            "known_seq": known,
            "token_budget": budget,
            "tokenizer": "utf8_bytes",
        },
        "query": query,
        "exact_key": None,
        "types": [],
        "limit": limit,
    }
