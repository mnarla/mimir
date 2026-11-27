"""One loopback service for scoped REST and MCP access to the local ledger."""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import stat
import sys
import tomllib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp import types
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from starlette.requests import ClientDisconnect, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mimir.budget import ReadError
from mimir.config import ConfigError, Settings, load_config
from mimir.contracts import MAX_BODY_BYTES, ContractError, _constant, _json_copy, _pairs
from mimir.db import DatabaseError, initialize_database, open_database
from mimir.embeddings import Embedder, Generation, LocalEmbedder, load_vec
from mimir.history import get_history
from mimir.ledger import LedgerError, record_memory
from mimir.mcp_server import READ_TOOLS, TOOL_DESCRIPTIONS, manager_for
from mimir.retrieval import get_project_context, search_memory
from mimir.review import resolve_conflict
from mimir.worker import run_once

DEFAULT_PORT = 8765
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}")
_STATUS = {
    "invalid_input": 400,
    "budget_too_small": 400,
    "unauthorized": 401,
    "scope_denied": 403,
    "not_found": 404,
    "version_conflict": 409,
    "idempotency_conflict": 409,
    "invalid_transition": 409,
    "resource_limit": 429,
    "unavailable": 503,
}
_LEDGER_ERRORS = {
    "Write scope denied": "scope_denied",
    "Project not provisioned": "not_found",
    "Idempotency key collision": "idempotency_conflict",
    "Cardinality change requires review": "version_conflict",
    "Ambiguous identity requires review": "version_conflict",
    "Expected revision mismatch": "version_conflict",
    "Superseded revision mismatch": "version_conflict",
    "TaskState requires effective ordering": "invalid_input",
    "TaskState order or revision mismatch": "invalid_transition",
    "Invalid TaskState transition": "invalid_transition",
    "Cannot reopen an absent task": "invalid_transition",
    "Index work capacity exceeded": "resource_limit",
}


@dataclass(frozen=True)
class Principal:
    id: str
    read: frozenset[str] = frozenset()
    write: frozenset[str] = frozenset()
    review: frozenset[str] = frozenset()
    admin: bool = False


def _private_toml(path: Path) -> dict[str, Any]:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ConfigError("Credential file must be a private regular file")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ConfigError("Credential file too large")
    return tomllib.loads(raw.decode("utf-8"))


def load_principals(path: Path) -> dict[str, Principal]:
    try:
        parsed = _private_toml(path)
    except (OSError, ValueError):
        raise ConfigError("Cannot load private credential file") from None
    if set(parsed) != {"principals"} or not isinstance(parsed["principals"], dict):
        raise ConfigError("Invalid credential file")
    output = {}
    for name, entry in parsed["principals"].items():
        if (
            not _ID.fullmatch(name)
            or not isinstance(entry, dict)
            or set(entry) - {"token_sha256", "read", "write", "review", "admin"}
        ):
            raise ConfigError("Invalid principal")
        digest = entry.get("token_sha256")
        admin = entry.get("admin", False)
        if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
            raise ConfigError("Invalid credential digest")
        if digest in output or type(admin) is not bool:
            raise ConfigError("Invalid principal grants")
        grants = []
        for capability in ("read", "write", "review"):
            projects = entry.get(capability, [])
            if not isinstance(projects, list) or any(
                not isinstance(project, str) or not _ID.fullmatch(project) for project in projects
            ):
                raise ConfigError("Invalid project grants")
            grants.append(frozenset(projects))
        output[digest] = Principal(name, grants[0], grants[1], grants[2], admin)
    if not output:
        raise ConfigError("At least one principal is required")
    return output


def _error(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": code.replace("_", " ").capitalize(),
            "retryable": code in {"resource_limit", "unavailable"},
        },
    }


def _response(code: str, status: int | None = None) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if code == "unauthorized" else None
    return JSONResponse(_error(code), status_code=status or _STATUS[code], headers=headers)


class LocalGate:
    def __init__(
        self, app: ASGIApp, *, principals: Mapping[str, Principal], port: int, mcp: ASGIApp
    ) -> None:
        self.app = app
        self.mcp = mcp
        self.principals = principals
        self.host = f"127.0.0.1:{port}"
        self.inflight = anyio.CapacityLimiter(16)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        request = Request(scope, receive)
        hosts = request.headers.getlist("host")
        origins = request.headers.getlist("origin")
        try:
            local = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            )
        except ValueError:
            local = False
        if not local or hosts != [self.host] or origins not in ([], [f"http://{self.host}"]):
            await _response("scope_denied")(scope, receive, no_store)
            return
        auth = request.headers.getlist("authorization")
        token = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
        principal = (
            self.principals.get(hashlib.sha256(token.encode()).hexdigest())
            if (_TOKEN.fullmatch(token))
            else None
        )
        if principal is None:
            await _response("unauthorized")(scope, receive, no_store)
            return
        scope["mimir.principal"] = principal

        try:
            self.inflight.acquire_nowait()
        except anyio.WouldBlock:
            await _response("resource_limit")(scope, receive, no_store)
            return
        try:
            await self._dispatch(scope, request, receive, no_store)
        finally:
            self.inflight.release()

    async def _dispatch(self, scope: Scope, request: Request, receive: Receive, send: Send) -> None:
        is_mcp = scope["path"] == "/mcp"
        if is_mcp:
            versions = request.headers.getlist("mcp-protocol-version")
            if versions and (len(versions) != 1 or versions[0] not in SUPPORTED_PROTOCOL_VERSIONS):
                await self._mcp_error(scope, receive, send)
                return
        if request.method == "POST":
            declared = request.headers.getlist("content-length")
            if declared and (len(declared) != 1 or not re.fullmatch(r"[0-9]{1,20}", declared[0])):
                await _response("invalid_input")(scope, receive, send)
                return
            if declared and int(declared[0]) > MAX_BODY_BYTES:
                await _response("resource_limit", 413)(scope, receive, send)
                return
            body = bytearray()
            try:
                with anyio.fail_after(5):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > MAX_BODY_BYTES:
                            await _response("resource_limit", 413)(scope, receive, send)
                            return
                        body.extend(chunk)
            except ClientDisconnect:
                return
            except TimeoutError:
                await _response("resource_limit", 408)(scope, receive, send)
                return
            if is_mcp:
                try:
                    decoded = _json_copy(
                        json.loads(
                            body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
                        )
                    )
                    parsed = types.JSONRPCMessage.model_validate(decoded).root
                    if isinstance(parsed, types.JSONRPCRequest):
                        types.ClientRequest.model_validate(parsed.model_dump())
                    elif isinstance(parsed, types.JSONRPCNotification):
                        types.ClientNotification.model_validate(parsed.model_dump())
                except (ValueError, RecursionError):
                    await self._mcp_error(scope, receive, send)
                    return

            pending = True

            async def replay() -> Message:
                nonlocal pending
                if pending:
                    pending = False
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await (self.mcp if is_mcp else self.app)(scope, replay, send)
        else:
            await (self.mcp if is_mcp else self.app)(scope, receive, send)

    @staticmethod
    async def _mcp_error(scope: Scope, receive: Receive, send: Send) -> None:
        error = JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid MCP message"},
            },
            status_code=400,
        )
        await error(scope, receive, send)


def create_app(
    settings: Settings,
    principals: Mapping[str, Principal],
    *,
    port: int = DEFAULT_PORT,
    embedder: Embedder | None = None,
) -> FastAPI:
    if not 1 <= port <= 65535 or not principals:
        raise ConfigError("Invalid local service settings")
    writes = anyio.Lock()

    def connect() -> sqlite3.Connection:
        return open_database(
            settings.data_dir,
            busy_timeout_ms=settings.busy_timeout_ms,
            cache_kib=settings.cache_kib,
        )

    def invoke(principal: Principal, name: str, payload: object) -> dict[str, Any]:
        try:
            with closing(connect()) as db:
                match name:
                    case "record_memory":
                        return record_memory(
                            db,
                            payload,
                            principal_id=principal.id,
                            authorized_projects=principal.write,
                        )
                    case "resolve_conflict":
                        return resolve_conflict(
                            db, payload, principal_id=principal.id, review_projects=principal.review
                        )
                    case "get_history":
                        return get_history(db, payload, authorized_projects=principal.read)
                    case "search_memory":
                        return search_memory(
                            db, payload, authorized_projects=principal.read, embedder=embedder
                        )
                    case "get_project_context":
                        return get_project_context(
                            db, payload, authorized_projects=principal.read, embedder=embedder
                        )
        except ContractError:
            return _error("invalid_input")
        except ReadError as exc:
            return _error(exc.code if exc.code in _STATUS else "unavailable")
        except LedgerError as exc:
            return _error(_LEDGER_ERRORS.get(str(exc), "unavailable"))
        except (DatabaseError, sqlite3.Error):
            return _error("unavailable")
        return _error("not_found")

    async def dispatch(request: Request, name: str, payload: object) -> dict[str, Any]:
        principal = request.scope.get("mimir.principal")
        if not isinstance(principal, Principal):
            return _error("unauthorized")
        if name not in TOOL_DESCRIPTIONS:
            return _error("not_found")
        run = partial(invoke, principal, name, payload)
        if name in READ_TOOLS:
            return await anyio.to_thread.run_sync(run)
        async with writes:
            return await anyio.to_thread.run_sync(run)

    manager = manager_for(dispatch)

    def index_once(active: Embedder) -> bool:
        with closing(connect()) as db:
            return run_once(db, active)

    async def index(active: Embedder) -> None:
        while True:
            try:
                worked = await anyio.to_thread.run_sync(index_once, active)
            except (DatabaseError, sqlite3.Error, ReadError):
                worked = False
            await anyio.sleep(0.05 if worked else 1)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with closing(
            initialize_database(
                settings.data_dir,
                busy_timeout_ms=settings.busy_timeout_ms,
                cache_kib=settings.cache_kib,
            )
        ) as db:
            vector_available = embedder is not None and load_vec(db)
        fd = os.open(
            settings.data_dir / "service.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ConfigError("Invalid service lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigError("A service already owns this memory store") from None
            async with manager.run(), anyio.create_task_group() as tasks:
                if embedder is not None and vector_available:
                    tasks.start_soon(index, embedder)
                try:
                    yield
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            os.close(fd)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        LocalGate, principals=dict(principals), port=port, mcp=manager.handle_request
    )

    @app.post("/v1/tools/{name}")
    async def call(name: str, request: Request) -> JSONResponse:
        result = await dispatch(request, name, await request.body())
        return JSONResponse(
            result, status_code=200 if result["ok"] else _STATUS[result["error"]["code"]]
        )

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        if not request.scope["mimir.principal"].admin:
            return _response("scope_denied")

        def probe() -> dict[str, Any]:
            with closing(connect()) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                semantic = "disabled"
                if embedder is not None and load_vec(db):
                    current = db.execute(
                        "SELECT id,model,artifact_digest,dimensions,normalization,tokenizer_digest "
                        "FROM embedding_generation WHERE id=? AND enabled=1",
                        (embedder.generation.id,),
                    ).fetchone()
                    if current == embedder.generation.values():
                        pending = db.execute(
                            "SELECT 1 FROM accepted_segment a WHERE a.known_to_seq IS NULL "
                            "AND NOT EXISTS (SELECT 1 FROM revision_embedding e "
                            "WHERE e.revision_id=a.revision_id AND e.generation_id=? "
                            "AND e.dimensions=?) LIMIT 1",
                            (embedder.generation.id, embedder.generation.dimensions),
                        ).fetchone()
                        semantic = "lagging" if pending else "ready"
                return {"status": "ready", "schema_version": version, "semantic": semantic}

        try:
            checked = await anyio.to_thread.run_sync(probe)
        except (DatabaseError, sqlite3.Error):
            return _response("unavailable")
        return JSONResponse(checked)

    return app


def configured_embedder(settings: Settings, manifest_path: Path | None) -> LocalEmbedder | None:
    if manifest_path is None and settings.semantic_generation is None:
        return None
    if (
        manifest_path is None
        or settings.semantic_generation is None
        or settings.semantic_model_dir is None
    ):
        raise ConfigError("Semantic configuration requires a manifest and model directory")
    with manifest_path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ConfigError("Invalid semantic manifest")
    manifest = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(manifest, dict) or any(
        not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest)
        for digest in manifest.values()
    ):
        raise ConfigError("Invalid semantic manifest")
    with closing(
        initialize_database(
            settings.data_dir,
            busy_timeout_ms=settings.busy_timeout_ms,
            cache_kib=settings.cache_kib,
        )
    ) as db:
        row = db.execute(
            "SELECT id,model,artifact_digest,dimensions,normalization,tokenizer_digest "
            "FROM embedding_generation WHERE id=? AND enabled=1",
            (settings.semantic_generation,),
        ).fetchone()
    if row is None:
        raise ConfigError("Semantic generation is not registered")
    return LocalEmbedder(Generation(*row), settings.semantic_model_dir, manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Mimir loopback service")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--semantic-manifest", type=Path)
    args = parser.parse_args()
    try:
        settings = load_config(args.config, args.data_dir)
        principals = load_principals(args.credentials)
        app = create_app(
            settings,
            principals,
            port=args.port,
            embedder=configured_embedder(settings, args.semantic_manifest),
        )
    except (OSError, ValueError, DatabaseError):
        print("Invalid Mimir service configuration", file=sys.stderr)
        return 2
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            workers=1,
            proxy_headers=False,
            access_log=False,
            log_level="warning",
            timeout_keep_alive=5,
        )
    )
    server.run()
    return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
