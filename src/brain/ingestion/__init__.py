"""Explicit local JSONL discovery. The parsers return metadata and digests only."""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from brain.contracts import _constant, _json_copy, _pairs
from brain.privacy import scrub_text

MAX_LINE_BYTES = 262144
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")


class Malformed(ValueError):
    """The next complete line cannot be safely consumed."""


@dataclass(frozen=True)
class Event:
    session: str
    event_id: str
    timestamp: str
    role: str
    digest: str


def event_id(value: object) -> str:
    if not isinstance(value, str) or not _SESSION.fullmatch(value) or scrub_text(value) != value:
        raise Malformed("Invalid source identity")
    return value


def parse_line(raw: bytes) -> dict[str, Any]:
    try:
        if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
            raise Malformed("Invalid source record length")
        value = _json_copy(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        )
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise Malformed("Invalid source record type")
        return value
    except (ValueError, RecursionError):
        raise Malformed("Invalid source JSONL record") from None


def event(
    session: str, identity: str, timestamp: object, role: str, fragments: Iterable[str]
) -> Event | None:
    texts = [scrub_text(text) for text in fragments if text.strip()]
    if not texts:
        return None
    if not isinstance(timestamp, str) or not _STAMP.fullmatch(timestamp):
        raise Malformed("Invalid source timestamp")
    try:
        normalized = datetime.fromisoformat(timestamp).isoformat(timespec="microseconds")
        canonical = json.dumps([normalized, role, texts], ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (ValueError, UnicodeError):
        raise Malformed("Invalid source timestamp or text") from None
    return Event(session, identity, normalized.replace("+00:00", "Z"), role, digest)


def text_blocks(blocks: object, accepted: frozenset[str]) -> list[str]:
    if not isinstance(blocks, list):
        raise Malformed("Invalid message content")
    texts = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise Malformed("Invalid content block")
        if block["type"] in accepted:
            if not isinstance(block.get("text"), str):
                raise Malformed("Invalid text block")
            texts.append(block["text"])
    return texts
