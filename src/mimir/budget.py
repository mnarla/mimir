"""Canonical whole-data budgets and bounded database snapshots."""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Set
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


class ReadError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True)
class TokenCounter:
    identifier: str
    count: Callable[[bytes], int]


def metadata(limit: int, counter: TokenCounter | None) -> dict[str, Any]:
    return dict(
        limit=limit,
        used=0,
        counter=counter.identifier if counter else "utf8_bytes",
        counting="exact" if counter else "utf8_upper_bound",
        scope="canonical_data_json",
        truncated=False,
    )


def recount(data: dict[str, Any], counter: TokenCounter | None) -> bool:
    for _ in range(32):
        used = counter.count(canonical(data)) if counter else len(canonical(data))
        if type(used) is not int or used < 0:
            raise ReadError("unavailable")
        if used == data["budget"]["used"]:
            return bool(used <= data["budget"]["limit"])
        data["budget"]["used"] = used
    raise ReadError("unavailable")


def authorize(projects: list[str], allowed: Set[str]) -> None:
    if not set(projects) <= allowed:
        raise ReadError("scope_denied")


@contextmanager
def snapshot(db: sqlite3.Connection, *, write: bool = False) -> Iterator[None]:
    if db.in_transaction:
        raise ReadError("unavailable")
    deadline = time.monotonic() + 2
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise ReadError("unavailable") from None
    except Exception:
        db.rollback()
        raise
    finally:
        db.set_progress_handler(None, 0)


def source_from(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(
        zip(
            ("source_tool", "source_agent", "session_id", "timestamp", "anchor", "locator"),
            row,
            strict=True,
        )
    )


SOURCE_COLUMNS = "s.source_tool,s.source_agent,s.session_id,s.source_timestamp,s.anchor,s.locator"
