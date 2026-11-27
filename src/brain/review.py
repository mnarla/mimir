"""Explicitly authorized conflict decisions with immutable temporal evidence."""

import json
import re
import sqlite3
from collections.abc import Set
from datetime import UTC, datetime
from typing import Any

from brain.budget import ReadError, authorize, snapshot
from brain.contracts import prepare_input, validate_output
from brain.ledger import _hash, _id, _json
from brain.privacy import scrub_text


def resolve_conflict(
    db: sqlite3.Connection, payload: object, *, principal_id: str, review_projects: Set[str]
) -> dict[str, Any]:
    request = prepare_input("resolve_conflict", payload)
    authorize([request["project_id"]], review_projects)
    if (
        not isinstance(principal_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", principal_id)
        or scrub_text(principal_id) != principal_id
    ):
        raise ReadError("unauthorized")
    with snapshot(db, write=True):
        project = request["project_id"]
        receipt = db.execute(
            "SELECT request_hash,response_json FROM idempotency_receipt "
            "WHERE project_id=? AND principal_id=? AND operation='resolve_conflict' "
            "AND request_key=?",
            (project, principal_id, request["idempotency_key"]),
        ).fetchone()
        if receipt:
            if receipt[0] != _hash(request):
                raise ReadError("idempotency_conflict")
            return dict(json.loads(receipt[1]))
        conflict = db.execute(
            "SELECT memory_id,accepted_revision_id,candidate_revision_id,head_seq,status "
            "FROM conflict_head WHERE project_id=? AND id=?",
            (project, request["conflict_id"]),
        ).fetchone()
        if not conflict:
            raise ReadError("not_found")
        memory, existing, candidate, head, status = conflict
        if head != request["expected_conflict_seq"] or status != "pending_review":
            raise ReadError("version_conflict")
        current = db.execute(
            "SELECT revision_id FROM ledger_event WHERE project_id=? AND memory_id=? "
            "AND event='accept' ORDER BY seq DESC LIMIT 1",
            (project, memory),
        ).fetchone()
        if current is None or current[0] != existing:
            raise ReadError("version_conflict")
        old = db.execute(
            "SELECT valid_from,valid_to FROM memory_revision WHERE id=?", (existing,)
        ).fetchone()
        new = db.execute(
            "SELECT valid_from,valid_to FROM memory_revision WHERE id=?", (candidate,)
        ).fetchone()
        cardinality = db.execute("SELECT cardinality FROM memory WHERE id=?", (memory,)).fetchone()[
            0
        ]
        action = request["action"]
        effective = request["effective_at"]
        if action == "accept_candidate" and (
            new[0] is None or effective < new[0] or (new[1] is not None and effective >= new[1])
        ):
            raise ReadError("invalid_input")
        if action == "coexist" and cardinality == "single":
            disjoint = (
                old[0] is not None
                and new[0] is not None
                and (
                    (old[1] is not None and old[1] <= new[0])
                    or (new[1] is not None and new[1] <= old[0])
                )
            )
            if not disjoint:
                raise ReadError("invalid_input")
        now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

        def event(name: str, revision: str, related: str | None = None) -> int:
            cursor = db.execute(
                "INSERT INTO ledger_event(project_id,memory_id,revision_id,event,"
                "effective_at,recorded_at,principal_id,related_revision_id,conflict_id,"
                "details_json,summary) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project,
                    memory,
                    revision,
                    name,
                    effective,
                    now,
                    principal_id,
                    related,
                    request["conflict_id"],
                    _json(
                        dict(
                            action=action,
                            rationale=request["rationale"],
                            accepted_revision=existing,
                            candidate_revision=candidate,
                            effective_at=effective,
                        )
                    ),
                    "Conflict review " + action,
                ),
            )
            if cursor.lastrowid is None:
                raise ReadError("unavailable")
            return cursor.lastrowid

        accepted = [existing]
        if action == "accept_candidate":
            close_seq = event("close", existing, candidate)
            segments = db.execute(
                "SELECT id,revision_id,valid_from,valid_to FROM accepted_segment "
                "WHERE project_id=? AND memory_id=? AND known_to_seq IS NULL",
                (project, memory),
            ).fetchall()
            for segment_id, revision, start, end in segments:
                if end is not None and end <= effective:
                    continue
                db.execute(
                    "UPDATE accepted_segment SET known_to_seq=? WHERE id=?", (close_seq, segment_id)
                )
                if start is not None and start < effective:
                    db.execute(
                        "INSERT INTO accepted_segment(project_id,memory_id,revision_id,"
                        "valid_from,valid_to,known_from_seq) VALUES (?,?,?,?,?,?)",
                        (project, memory, revision, start, effective, close_seq),
                    )
            supersede_seq = event("supersede", candidate, existing)
            db.execute(
                "INSERT INTO relation_event VALUES (?,?,?,?,?,?,?,?)",
                (
                    _id(),
                    supersede_seq,
                    project,
                    candidate,
                    "supersedes",
                    "revision",
                    existing,
                    "assert",
                ),
            )
            accepted = [candidate]
        if action in ("accept_candidate", "coexist"):
            accept_seq = event("accept", candidate)
            db.execute(
                "INSERT INTO accepted_segment(project_id,memory_id,revision_id,valid_from,"
                "valid_to,known_from_seq) VALUES (?,?,?,?,?,?)",
                (
                    project,
                    memory,
                    candidate,
                    effective if action == "accept_candidate" else new[0],
                    new[1],
                    accept_seq,
                ),
            )
            if action == "coexist":
                accepted.append(candidate)
        resolution = event("conflict_resolve", candidate, existing)
        db.execute(
            "UPDATE conflict_head SET status='resolved',head_seq=? WHERE id=?",
            (resolution, request["conflict_id"]),
        )
        if action == "accept_candidate":
            db.execute(
                "UPDATE conflict_head SET accepted_revision_id=?,head_seq=? "
                "WHERE memory_id=? AND status='pending_review'",
                (candidate, resolution, memory),
            )
        result = dict(
            ok=True,
            data=dict(
                conflict_id=request["conflict_id"],
                resolution_seq=resolution,
                accepted_revisions=accepted,
                status="resolved",
            ),
        )
        validate_output("resolve_conflict", result)
        db.execute(
            "INSERT INTO idempotency_receipt VALUES (?,?,?,?,?,?,?)",
            (
                project,
                principal_id,
                "resolve_conflict",
                request["idempotency_key"],
                _hash(request),
                _json(result),
                now,
            ),
        )
        return result
