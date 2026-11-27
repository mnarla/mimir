"""Codex rollout JSONL message metadata, excluding tools and system content."""

from collections.abc import Set
from typing import Any

from brain.ingestion import Event, Malformed, event, event_id, text_blocks


def session_header(line: dict[str, Any]) -> str:
    payload = line.get("payload")
    if line["type"] != "session_meta" or not isinstance(payload, dict):
        raise Malformed("Expected Codex session metadata")
    metadata = payload.get("meta", payload)
    if not isinstance(metadata, dict):
        raise Malformed("Invalid Codex session metadata")
    return event_id(metadata.get("id"))


def message(
    line: dict[str, Any], session: str, offset: int, excluded_sessions: Set[str]
) -> Event | None:
    if line["type"] == "session_meta":
        if session_header(line) != session:
            raise Malformed("Unexpected Codex session change")
        return None
    if line["type"] != "response_item" or session in excluded_sessions:
        return None
    payload = line.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise Malformed("Invalid Codex response item")
    if payload["type"] != "message" or payload.get("role") not in ("user", "assistant"):
        return None
    ordinal = line.get("ordinal")
    identity = (
        f"ordinal:{ordinal}" if type(ordinal) is int and 0 <= ordinal < 2**64 else f"byte:{offset}"
    )
    return event(
        session,
        identity,
        line.get("timestamp"),
        payload["role"],
        text_blocks(payload.get("content"), frozenset({"input_text", "output_text"})),
    )
