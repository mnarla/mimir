"""Expose the existing Mimir contracts over MCP Streamable HTTP."""

from collections.abc import Awaitable, Callable
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request

from mimir.contracts import schema_for

TOOL_DESCRIPTIONS = {
    "search_memory": "Search accepted project memories within an authorized scope.",
    "get_project_context": "Retrieve a budgeted context pack for a project.",
    "get_history": "Read a bounded page of a memory's history.",
    "record_memory": "Record a structured memory with source metadata.",
    "resolve_conflict": "Review a pending conflict; requires separate review permission.",
}
READ_TOOLS = frozenset({"search_memory", "get_project_context", "get_history"})


def manager_for(
    dispatch: Callable[[Request, str, object], Awaitable[dict[str, Any]]],
) -> StreamableHTTPSessionManager:
    server: Server[Any] = Server("mimir", version="0.1.0", website_url=None)
    tools = [
        types.Tool(
            name=name,
            description=description,
            inputSchema=schema_for(name, "Input", compact=True),
            outputSchema=schema_for(name, "Output", compact=True),
            annotations=types.ToolAnnotations(readOnlyHint=name in READ_TOOLS),
        )
        for name, description in TOOL_DESCRIPTIONS.items()
    ]

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        request = server.request_context.request
        if not isinstance(request, Request):
            return types.CallToolResult(
                content=[],
                isError=True,
                structuredContent={
                    "ok": False,
                    "error": {
                        "code": "unauthorized",
                        "message": "Unauthorized",
                        "retryable": False,
                    },
                },
            )
        result = await dispatch(request, name, arguments)
        return types.CallToolResult(content=[], structuredContent=result, isError=not result["ok"])

    return StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)
