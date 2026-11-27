"""Claude Code JSONL message metadata, excluding sidechains and tool blocks."""

from collections.abc import Set
from typing import Any

from mimir.ingestion import Event, Malformed, event, event_id, text_blocks


def message(line: dict[str, Any], excluded_sessions: Set[str]) -> Event | None:
    role = line["type"]
    if role not in ("user", "assistant") or line.get("isSidechain") is True:
        return None
    session = event_id(line.get("sessionId"))
    if session in excluded_sessions:
        return None
    identity = event_id(line.get("uuid"))
    payload = line.get("message")
    if not isinstance(payload, dict) or payload.get("role") != role:
        raise Malformed("Invalid Claude Code message")
    content = payload.get("content")
    texts = [content] if isinstance(content, str) else text_blocks(content, frozenset({"text"}))
    return event(session, identity, line.get("timestamp"), role, texts)
