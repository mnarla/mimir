"""Ordered, snapshot-pinned ledger history with whole-event continuation."""

import sqlite3
from collections.abc import Set
from typing import Any

from mimir.budget import (
    ReadError,
    TokenCounter,
    authorize,
    metadata,
    recount,
    snapshot,
    source_from,
)
from mimir.contracts import prepare_input, validate_output


def get_history(
    db: sqlite3.Connection,
    payload: object,
    *,
    authorized_projects: Set[str],
    counter: TokenCounter | None = None,
) -> dict[str, Any]:
    request = prepare_input("get_history", payload, configured_tokenizer=counter is not None)
    counter = counter if request["tokenizer"] == "configured" else None
    authorize([request["project_id"]], authorized_projects)
    with snapshot(db):
        if not db.execute(
            "SELECT 1 FROM memory WHERE project_id=? AND id=?",
            (request["project_id"], request["memory_id"]),
        ).fetchone():
            raise ReadError("not_found")
        upper = db.execute("SELECT coalesce(max(seq),0) FROM ledger_event").fetchone()[0]
        through = upper if request["through_seq"] is None else request["through_seq"]
        if through > upper or request["after_seq"] > through:
            raise ReadError("invalid_input")
        rows = db.execute(
            "SELECT e.seq,e.event,e.memory_id,e.revision_id,e.recorded_at,e.effective_at,"
            "e.summary,s.source_tool,s.source_agent,s.session_id,s.source_timestamp,s.anchor,"
            "s.locator FROM ledger_event e LEFT JOIN source_ref s "
            "ON s.id=e.source_ref_id WHERE e.project_id=? AND e.memory_id=? "
            "AND e.seq>? AND e.seq<=? ORDER BY e.seq LIMIT ?",
            (
                request["project_id"],
                request["memory_id"],
                request["after_seq"],
                through,
                request["limit"] + 1,
            ),
        ).fetchall()
        data = dict(
            through_seq=through,
            events=[],
            next_after_seq=None,
            budget=metadata(request["token_budget"], counter),
        )
        for index, row in enumerate(rows):
            event = dict(
                zip(
                    (
                        "seq",
                        "event",
                        "memory_id",
                        "revision_id",
                        "recorded_at",
                        "effective_at",
                        "summary",
                    ),
                    row[:7],
                    strict=True,
                )
            )
            event["source"] = source_from(row[7:]) if row[7] is not None else None
            data["events"].append(event)
            data["next_after_seq"] = row[0] if index < len(rows) - 1 else None
            if len(data["events"]) > request["limit"] or not recount(data, counter):
                data["events"].pop()
                data["budget"]["truncated"] = True
                if not data["events"]:
                    raise ReadError("budget_too_small")
                data["next_after_seq"] = data["events"][-1]["seq"]
                break
        while not recount(data, counter):
            if len(data["events"]) <= 1:
                raise ReadError("budget_too_small")
            data["events"].pop()
            data["next_after_seq"] = data["events"][-1]["seq"]
        result = dict(ok=True, data=data)
        validate_output("get_history", result)
        return result
