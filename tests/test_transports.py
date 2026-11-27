"""HTTP and MCP share one synthetic local ledger."""

import hashlib
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import anyio
import httpx
import uvicorn
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from test_contracts import record

from brain.budget import canonical
from brain.config import ConfigError, Settings
from brain.contracts import MAX_BODY_BYTES, schema_for
from brain.db import initialize_database
from brain.service import Principal, create_app, load_principals

BASE = "http://127.0.0.1:8765"
WRITER = "w" * 43
READER = "r" * 43


class TransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(Path(temporary.name) / "data")
        with initialize_database(self.settings.data_dir) as db:
            db.execute("INSERT INTO project VALUES ('p_test','now','Synthetic')")
        db.close()
        self.principals = {
            hashlib.sha256(WRITER.encode()).hexdigest(): Principal(
                "writer", read=frozenset({"p_test"}), write=frozenset({"p_test"}), admin=True
            ),
            hashlib.sha256(READER.encode()).hexdigest(): Principal("reader"),
        }
        self.client = self.enterContext(
            TestClient(
                create_app(self.settings, self.principals),
                base_url=BASE,
                client=("127.0.0.1", 12345),
            )
        )
        self.auth = {"Authorization": f"Bearer {WRITER}"}

    def test_duplicate_service_and_private_credential_file(self):
        credentials = self.settings.data_dir.parent / "grants.toml"
        digest = next(iter(self.principals))
        credentials.write_text(
            f'[principals.writer]\ntoken_sha256="{digest}"\n'
            'read=["p_test"]\nwrite=["p_test"]\nadmin=true\n'
        )
        credentials.chmod(0o644)
        with self.assertRaises(ConfigError):
            load_principals(credentials)
        credentials.chmod(0o600)
        self.assertEqual(load_principals(credentials), {digest: self.principals[digest]})
        with self.assertRaisesRegex(ConfigError, "already owns"):
            with TestClient(create_app(self.settings, self.principals), base_url=BASE):
                self.fail("Second service acquired the store")

    def test_stdio_relay_and_streamable_http_sdk_on_loopback(self):
        settings = Settings(self.settings.data_dir.parent / "network")
        db = initialize_database(settings.data_dir)
        with db:
            db.execute("INSERT INTO project VALUES ('p_test','now','Synthetic')")
        db.close()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            endpoint = f"http://127.0.0.1:{port}/mcp"
            app = create_app(settings, self.principals, port=port)
            server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
            worker = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.started)

                async def exercise():
                    with anyio.fail_after(10):
                        async with httpx.AsyncClient(headers=self.auth, trust_env=False) as http:
                            async with streamable_http_client(
                                endpoint, http_client=http
                            ) as streams:
                                async with ClientSession(streams[0], streams[1]) as session:
                                    await session.initialize()
                                    written = await session.call_tool("record_memory", record())
                                    self.assertFalse(written.isError)
                        params = StdioServerParameters(
                            command=sys.executable,
                            args=["-m", "brain.stdio", "--port", str(port)],
                            env={"BRAIN_TOKEN": WRITER},
                        )
                        async with stdio_client(params) as streams:
                            async with ClientSession(*streams) as session:
                                await session.initialize()
                                self.assertEqual(len((await session.list_tools()).tools), 5)
                                scope = dict(
                                    project_id="p_test",
                                    additional_projects=[],
                                    valid_at=None,
                                    known_seq=None,
                                    token_budget=16384,
                                    tokenizer="utf8_bytes",
                                )
                                pack = await session.call_tool(
                                    "get_project_context", {"scope": scope, "goal": None}
                                )
                                self.assertFalse(pack.isError)
                                items = pack.structuredContent["data"]["items"]
                                self.assertEqual(items[0]["statement"], "Use SQLite")

                anyio.run(exercise)
            finally:
                server.should_exit = True
                worker.join(5)
            self.assertFalse(worker.is_alive())

    def rpc(self, method, params=None, token=WRITER, version="2025-11-25"):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        return self.client.post(
            "/mcp",
            json=message,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": version,
            },
        )

    def test_http_write_mcp_read_and_discovery(self):
        initialized = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "synthetic", "version": "1"},
            },
        )
        self.assertEqual(initialized.json()["result"]["protocolVersion"], "2025-11-25")
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual(len(tools), 5)
        self.assertLess(len(canonical(tools)), 30_000, "MCP catalog includes unused definitions")
        for tool in tools:
            for field, direction in (("inputSchema", "Input"), ("outputSchema", "Output")):
                with self.subTest(tool=tool["name"], direction=direction):
                    full = schema_for(tool["name"], direction)
                    advertised = tool[field]
                    references = {
                        name.decode()
                        for name in re.findall(
                            rb'"\$ref":"#/\$defs/([^"]+)"', canonical(advertised)
                        )
                    }
                    self.assertEqual(set(advertised["$defs"]), references)
                    expected = {name: full["$defs"][name] for name in references}
                    self.assertEqual(advertised, {**full, "$defs": expected})
                    Draft202012Validator.check_schema(advertised)

        writer = next(tool for tool in tools if tool["name"] == "record_memory")
        reader = next(tool for tool in tools if tool["name"] == "search_memory")
        write_schema = Draft202012Validator(writer["inputSchema"])
        self.assertTrue(write_schema.is_valid(record()))
        self.assertFalse(write_schema.is_valid({**record(), "unexpected": True}))
        ack = self.client.post("/v1/tools/record_memory", json=record(), headers=self.auth)
        self.assertEqual(ack.status_code, 200)
        query = dict(
            scope=dict(
                project_id="p_test",
                additional_projects=[],
                valid_at=None,
                known_seq=None,
                token_budget=16384,
                tokenizer="utf8_bytes",
            ),
            query="SQLite",
            exact_key=None,
            types=[],
            limit=10,
        )
        response = self.rpc("tools/call", {"name": "search_memory", "arguments": query})
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"], [])
        Draft202012Validator(reader["outputSchema"]).validate(result["structuredContent"])
        self.assertEqual(result["structuredContent"]["data"]["items"][0]["statement"], "Use SQLite")
        denied = self.rpc("tools/call", {"name": "search_memory", "arguments": query}, token=READER)
        self.assertEqual(
            denied.json()["result"]["structuredContent"]["error"]["code"], "scope_denied"
        )

    def test_auth_headers_scope_and_resource_limits(self):
        path = "/v1/tools/record_memory"
        for headers, status in (
            ({}, 401),
            ({**self.auth, "Host": "example.test"}, 403),
            ({**self.auth, "Origin": "https://example.test"}, 403),
            ({"Authorization": f"Bearer {READER}"}, 403),
        ):
            response = self.client.post(path, json=record(), headers=headers)
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        self.assertEqual(
            self.client.get("/health", headers=self.auth).json()["semantic"], "disabled"
        )
        self.assertEqual(
            self.client.get("/health", headers={"Authorization": f"Bearer {READER}"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                path, content=b"x" * (MAX_BODY_BYTES + 1), headers=self.auth
            ).status_code,
            413,
        )
        self.assertEqual(
            self.client.post(
                path, content=iter([b"x" * MAX_BODY_BYTES, b"x"]), headers=self.auth
            ).status_code,
            413,
        )

    def test_strict_inputs_and_review_denied_to_writer(self):
        response = self.client.post(
            "/v1/tools/record_memory",
            headers=self.auth,
            content=b'{"secret":"synthetic","secret":"duplicate"}',
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("synthetic", response.text)
        invalid_version = self.rpc("tools/list", version="private-synthetic-header")
        self.assertEqual(invalid_version.status_code, 400)
        self.assertNotIn("private-synthetic-header", invalid_version.text)
        invalid = record()
        invalid["extra"] = "private-synthetic-text"
        result = self.rpc("tools/call", {"name": "record_memory", "arguments": invalid}).json()[
            "result"
        ]
        self.assertTrue(result["isError"])
        self.assertNotIn("private-synthetic-text", str(result))
        resolution = dict(
            project_id="p_test",
            conflict_id="fake",
            expected_conflict_seq=1,
            action="keep_existing",
            effective_at=None,
            idempotency_key="review",
            rationale="Synthetic",
        )
        self.assertEqual(
            self.client.post(
                "/v1/tools/resolve_conflict", json=resolution, headers=self.auth
            ).status_code,
            403,
        )
