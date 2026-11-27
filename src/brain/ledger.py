"""Structured writes only: append-only authority and rebuildable projections in one lock.

The caller supplies a trusted principal and its authorized project set. This is an
in-process boundary, not authentication; transports must obtain these from credentials.
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Set
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from brain.contracts import prepare_input, validate_output
from brain.privacy import scrub_text


class LedgerError(ValueError):
    """Fixed, content-free writer failure suitable for a future transport mapping."""


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _id() -> str:
    return uuid4().hex


def record_memory(
    connection: sqlite3.Connection,
    payload: object,
    *,
    principal_id: str,
    authorized_projects: Set[str],
    max_pending_work: int = 1000,
) -> dict[str, Any]:
    """Prepare and durably acknowledge one card, or leave the database unchanged.

    Idempotency hashes the canonical sanitized request. Source identity is the
    canonical sanitized Source object (structured writes have no ingest manifest).
    No source body, model inference or external I/O is used inside the transaction.
    """
    r = prepare_input("record_memory", payload)
    if (
        not isinstance(principal_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", principal_id)
        or scrub_text(principal_id) != principal_id
        or r["project_id"] not in authorized_projects
    ):
        raise LedgerError("Write scope denied")
    if type(max_pending_work) is not int or not 0 <= max_pending_work <= 100000:
        raise LedgerError("Invalid work capacity")
    if connection.in_transaction:
        raise LedgerError("Writer requires an idle connection")
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = _write(connection, r, principal_id, max_pending_work)
        validate_output("record_memory", result)
        connection.commit()
        return result
    except sqlite3.Error:
        connection.rollback()
        raise LedgerError("Structured write failed") from None
    except Exception:
        connection.rollback()
        raise


def _write(
    db: sqlite3.Connection, r: dict[str, Any], principal: str, max_pending_work: int
) -> dict[str, Any]:
    project = r["project_id"]
    if not db.execute("SELECT 1 FROM project WHERE id=?", (project,)).fetchone():
        raise LedgerError("Project not provisioned")
    request_hash = _hash(r)
    receipt = db.execute(
        "SELECT request_hash,response_json FROM idempotency_receipt "
        "WHERE project_id=? AND principal_id=? AND operation='record_memory' AND request_key=?",
        (project, principal, r["idempotency_key"]),
    ).fetchone()
    if receipt:
        if receipt[0] != request_hash:
            raise LedgerError("Idempotency key collision")
        return dict(json.loads(receipt[1]))

    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    identity = r["identity"]
    kind = r["payload"]["type"]
    # Claim identity deliberately includes prose and validity: overlap is not agreement.
    claim_hash = _hash([r[k] for k in ("statement", "payload", "valid_from", "valid_to")])
    memories = db.execute(
        "SELECT id,cardinality FROM memory WHERE project_id=? AND entity_key=? "
        "AND fact_key=? AND applicability_scope=? AND kind=? ORDER BY id",
        (project, identity["entity"], identity["key"], identity["scope"], kind),
    ).fetchall()
    if any(row[1] != r["cardinality"] for row in memories):
        raise LedgerError("Cardinality change requires review")
    matches = db.execute(
        "SELECT r.id,r.memory_id,r.version FROM memory_revision r JOIN memory m "
        "ON m.id=r.memory_id WHERE m.project_id=? AND m.entity_key=? AND m.fact_key=? "
        "AND m.applicability_scope=? AND m.kind=? AND r.normalized_hash=?",
        (project, identity["entity"], identity["key"], identity["scope"], kind, claim_hash),
    ).fetchall()
    if len(memories) > 1 or len(matches) > 1:
        raise LedgerError("Ambiguous identity requires review")
    memory = memories[0][0] if memories else _id()
    head = db.execute(
        "SELECT r.id,r.version,r.typed_json,r.valid_from,r.valid_to FROM ledger_event e "
        "JOIN memory_revision r ON r.id=e.revision_id WHERE e.project_id=? "
        "AND e.memory_id=? AND e.event='accept' ORDER BY e.seq DESC LIMIT 1",
        (project, memory),
    ).fetchone()
    if r["expected_revision"] is not None and (not head or r["expected_revision"] != head[0]):
        raise LedgerError("Expected revision mismatch")
    if r["supersedes_revision"] is not None and (not head or r["supersedes_revision"] != head[0]):
        raise LedgerError("Superseded revision mismatch")

    source = r["source"]
    source_hash = _hash(source)
    source_id = "structured:" + source_hash
    existing_source = db.execute(
        "SELECT id FROM source_ref WHERE project_id=? AND source_tool=? AND session_id=? "
        "AND source_id=? AND source_revision='1' AND parser_generation='structured-v1'",
        (project, source["source_tool"], source["session_id"], source_id),
    ).fetchone()
    source_ref = existing_source[0] if existing_source else _id()
    if not existing_source:
        db.execute(
            "INSERT INTO source_ref VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_ref,
                project,
                source["source_tool"],
                source["source_agent"],
                source["session_id"],
                source["timestamp"],
                source["anchor"],
                source["locator"],
                source_id,
                "1",
                source_hash,
                "structured-v1",
            ),
        )

    def event(
        name: str,
        revision: str,
        *,
        related: str | None = None,
        conflict: str | None = None,
        effective: str | None = None,
    ) -> int:
        cursor = db.execute(
            "INSERT INTO ledger_event(project_id,memory_id,revision_id,event,effective_at,"
            "recorded_at,principal_id,source_ref_id,related_revision_id,conflict_id,"
            "details_json,summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                project,
                memory,
                revision,
                name,
                effective,
                now,
                principal,
                source_ref,
                related,
                conflict,
                "{}",
                "Structured memory " + name,
            ),
        )
        if cursor.lastrowid is None:
            raise LedgerError("Missing ledger sequence")
        return cursor.lastrowid

    def relation(seq: int, revision: str, name: str, target_kind: str, target: str) -> None:
        db.execute(
            "INSERT INTO relation_event VALUES (?,?,?,?,?,?,?,?)",
            (_id(), seq, project, revision, name, target_kind, target, "assert"),
        )

    conflict_id = None
    if matches:
        revision, memory, version = matches[0]
        previous = db.execute(
            "SELECT seq FROM ledger_event WHERE project_id=? AND revision_id=? "
            "AND source_ref_id=? AND event IN ('assert','support') ORDER BY seq DESC LIMIT 1",
            (project, revision, source_ref),
        ).fetchone()
        if previous:
            seq, outcome = previous[0], "noop"
        else:
            seq, outcome = event("support", revision), "supported"
            relation(seq, revision, "supported_by", "source", source_ref)
        pending = db.execute(
            "SELECT id FROM conflict_head WHERE candidate_revision_id=? "
            "AND status='pending_review'",
            (revision,),
        ).fetchone()
        conflict_id = pending[0] if pending else None
    else:
        outcome = "added"
        replace = False
        if kind == "TaskState":
            data = r["payload"]["data"]
            if r["valid_from"] is None:
                raise LedgerError("TaskState requires effective ordering")
            if head:
                if (
                    r["expected_revision"] != head[0]
                    or head[3] is None
                    or r["valid_from"] <= head[3]
                ):
                    raise LedgerError("TaskState order or revision mismatch")
                old = json.loads(head[2])["data"]["state"]
                new = data["state"]
                terminal = old in ("done", "cancelled")
                allowed = {
                    "todo": {"in_progress", "blocked", "cancelled"},
                    "in_progress": {"blocked", "done", "cancelled"},
                    "blocked": {"todo", "in_progress", "cancelled"},
                }
                if terminal:
                    valid = data["transition"] == "reopen" and new in ("todo", "in_progress")
                else:
                    valid = data["transition"] == "set" and new in allowed[old]
                if not valid:
                    raise LedgerError("Invalid TaskState transition")
                replace = True
            elif data["transition"] != "set":
                raise LedgerError("Cannot reopen an absent task")
        elif head:
            replace = (
                kind not in ("Decision", "Constraint")
                and r["supersedes_revision"] == head[0]
                and head[3] is not None
                and r["valid_from"] is not None
                and r["valid_from"] > head[3]
            )
            if not replace:
                outcome = "pending_review"
        if replace:
            outcome = "superseded"
        if not memories:
            db.execute(
                "INSERT INTO memory VALUES (?,?,?,?,?,?,?,?)",
                (
                    memory,
                    project,
                    identity["entity"],
                    identity["key"],
                    identity["scope"],
                    kind,
                    r["cardinality"],
                    now,
                ),
            )
        version = db.execute(
            "SELECT coalesce(max(version),0)+1 FROM memory_revision WHERE memory_id=?",
            (memory,),
        ).fetchone()[0]
        revision = _id()
        db.execute(
            "INSERT INTO memory_revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision,
                project,
                memory,
                version,
                r["statement"],
                _json(r["payload"]),
                claim_hash,
                "exact-v1",
                r["valid_from"],
                r["valid_to"],
                now,
                source_ref,
            ),
        )
        seq = event("assert", revision, effective=r["valid_from"])
        relation(seq, revision, "supported_by", "source", source_ref)
        relation(seq, revision, "belongs_to_project", "project", project)
        relation(seq, revision, "about_entity", "entity", identity["entity"])
        if outcome == "pending_review":
            if head is None:
                raise LedgerError("Missing accepted revision")
            conflict_id = _id()
            seq = event("conflict_open", revision, related=head[0], conflict=conflict_id)
            relation(seq, revision, "conflicts_with", "revision", head[0])
            db.execute(
                "INSERT INTO conflict_head VALUES (?,?,?,?,?,?,?,?)",
                (conflict_id, project, memory, head[0], revision, seq, seq, "pending_review"),
            )
        else:
            if replace:
                if head is None:
                    raise LedgerError("Missing accepted revision")
                seq = event("close", head[0], related=revision, effective=r["valid_from"])
                # Preserve prior knowledge, then retain the old world-time prefix.
                segments = db.execute(
                    "SELECT id,valid_from,valid_to FROM accepted_segment WHERE revision_id=? "
                    "AND known_to_seq IS NULL",
                    (head[0],),
                ).fetchall()
                for segment_id, start, end in segments:
                    db.execute(
                        "UPDATE accepted_segment SET known_to_seq=? WHERE id=?", (seq, segment_id)
                    )
                    boundary = min(end, r["valid_from"]) if end else r["valid_from"]
                    if start is not None and start < boundary:
                        db.execute(
                            "INSERT INTO accepted_segment(project_id,memory_id,revision_id,"
                            "valid_from,valid_to,known_from_seq) VALUES (?,?,?,?,?,?)",
                            (project, memory, head[0], start, boundary, seq),
                        )
                seq = event("supersede", revision, related=head[0], effective=r["valid_from"])
                relation(seq, revision, "supersedes", "revision", head[0])
            seq = event("accept", revision, effective=r["valid_from"])
            db.execute(
                "INSERT INTO accepted_segment(project_id,memory_id,revision_id,valid_from,"
                "valid_to,known_from_seq) VALUES (?,?,?,?,?,?)",
                (project, memory, revision, r["valid_from"], r["valid_to"], seq),
            )
        db.execute(
            "INSERT INTO search_doc(project_id,revision_id,statement,identity_text) "
            "VALUES (?,?,?,?)",
            (project, revision, r["statement"], _json(identity)),
        )
        generations = db.execute("SELECT id FROM embedding_generation WHERE enabled=1").fetchall()
        pending_count = db.execute(
            "SELECT count(*) FROM work_item WHERE state IN ('pending','leased')"
        ).fetchone()[0]
        if generations and pending_count + len(generations) > max_pending_work:
            raise LedgerError("Index work capacity exceeded")
        # One job per revision/generation; inference and retry leasing belong to Task 6.
        for (generation,) in generations:
            db.execute(
                "INSERT INTO work_item(project_id,kind,revision_id,source_ref_id,generation_id,"
                "dedup_key,state,not_before) VALUES (?,'embed',?,?,?,?, 'pending',?)",
                (project, revision, source_ref, generation, _hash([revision, generation]), now),
            )
    queued = db.execute(
        "SELECT 1 FROM work_item WHERE revision_id=? AND state IN ('pending','leased') LIMIT 1",
        (revision,),
    ).fetchone()
    result = {
        "ok": True,
        "data": {
            "memory_id": memory,
            "revision_id": revision,
            "version": version,
            "ledger_seq": seq,
            "outcome": outcome,
            "conflict_id": conflict_id,
            "index_state": "pending" if queued else "degraded",
        },
    }
    db.execute(
        "INSERT INTO idempotency_receipt VALUES (?,?,?,?,?,?,?)",
        (
            project,
            principal,
            "record_memory",
            r["idempotency_key"],
            request_hash,
            _json(result),
            now,
        ),
    )
    return result
