"""Bounded outbox leases; inference is always outside SQLite transactions."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mimir.budget import snapshot
from mimir.embeddings import Embedder, EmbeddingError, Generation, vector_blob
from mimir.ledger import _hash


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timezone required")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def register_generation(db: sqlite3.Connection, generation: Generation) -> None:
    """Explicit local administration; never rewrites an established generation's identity."""
    with snapshot(db, write=True):
        current = db.execute(
            "SELECT id,model,artifact_digest,dimensions,normalization,tokenizer_digest "
            "FROM embedding_generation WHERE id=?",
            (generation.id,),
        ).fetchone()
        if current is not None and current != generation.values():
            raise EmbeddingError("Generation identity collision")
        db.execute("UPDATE embedding_generation SET enabled=0 WHERE id<>?", (generation.id,))
        if current is None:
            db.execute(
                "INSERT INTO embedding_generation VALUES (?,?,?,?,?,?,1)", generation.values()
            )
        else:
            db.execute("UPDATE embedding_generation SET enabled=1 WHERE id=?", (generation.id,))


def backfill(
    db: sqlite3.Connection, generation_id: str, *, limit: int = 128, max_pending_work: int = 1000
) -> int:
    """Queue bounded references for existing accepted or pending revisions."""
    if (
        type(limit) is not int
        or not 1 <= limit <= 512
        or type(max_pending_work) is not int
        or not 0 <= max_pending_work <= 100000
    ):
        raise EmbeddingError("Invalid backfill bounds")
    with snapshot(db, write=True):
        if not db.execute(
            "SELECT 1 FROM embedding_generation WHERE id=? AND enabled=1", (generation_id,)
        ).fetchone():
            raise EmbeddingError("Generation unavailable")
        pending = db.execute(
            "SELECT count(*) FROM work_item WHERE state IN ('pending','leased')"
        ).fetchone()[0]
        capacity = max(0, min(limit, max_pending_work - pending))
        rows = db.execute(
            "SELECT r.project_id,r.id,r.source_ref_id FROM memory_revision r "
            "WHERE NOT EXISTS (SELECT 1 FROM work_item w WHERE w.revision_id=r.id "
            "AND w.generation_id=? AND w.kind='embed' AND w.state IN ('pending','leased')) AND "
            "NOT EXISTS (SELECT 1 FROM revision_embedding e WHERE e.revision_id=r.id "
            "AND e.generation_id=?) AND "
            "(EXISTS (SELECT 1 FROM accepted_segment a WHERE a.revision_id=r.id "
            "AND a.known_to_seq IS NULL) OR EXISTS (SELECT 1 FROM conflict_head c "
            "WHERE c.candidate_revision_id=r.id AND c.status='pending_review')) "
            "ORDER BY r.id LIMIT ?",
            (generation_id, generation_id, capacity),
        ).fetchall()
        for project, revision, source in rows:
            db.execute(
                "INSERT INTO work_item(project_id,kind,revision_id,source_ref_id,generation_id,"
                "dedup_key,state,not_before) VALUES (?,'embed',?,?,?,?,'pending',?) "
                "ON CONFLICT(dedup_key) DO UPDATE SET state='pending',attempts=0,"
                "not_before=excluded.not_before,lease_until=NULL",
                (
                    project,
                    revision,
                    source,
                    generation_id,
                    _hash([revision, generation_id]),
                    _time(datetime.now(UTC)),
                ),
            )
        return len(rows)


@dataclass(frozen=True)
class Lease:
    id: int
    project: str
    revision: str
    generation: Generation
    attempts: int
    expires: str
    statement: str


def claim(
    db: sqlite3.Connection, *, now: datetime | None = None, lease_seconds: int = 60
) -> Lease | None:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise ValueError("Invalid lease duration")
    moment = now or datetime.now(UTC)
    timestamp = _time(moment)
    expires = _time(moment + timedelta(seconds=lease_seconds))
    with snapshot(db, write=True):
        db.execute(
            "UPDATE work_item SET state='failed',lease_until=NULL WHERE kind='embed' "
            "AND state='leased' AND lease_until<=? AND attempts>=3",
            (timestamp,),
        )
        row = db.execute(
            "SELECT w.id,w.project_id,w.revision_id,w.attempts,r.statement,"
            "g.id,g.model,g.artifact_digest,g.dimensions,g.normalization,g.tokenizer_digest "
            "FROM work_item w JOIN memory_revision r ON r.id=w.revision_id "
            "JOIN embedding_generation g ON g.id=w.generation_id "
            "WHERE w.kind='embed' AND w.not_before<=? AND "
            "(w.state='pending' OR (w.state='leased' AND w.lease_until<=?)) "
            "ORDER BY w.id LIMIT 1",
            (timestamp, timestamp),
        ).fetchone()
        if row is None:
            return None
        db.execute(
            "UPDATE work_item SET state='leased',attempts=attempts+1,lease_until=? WHERE id=?",
            (expires, row[0]),
        )
        return Lease(row[0], row[1], row[2], Generation(*row[5:]), row[3] + 1, expires, row[4])


def _owns(db: sqlite3.Connection, lease: Lease, now: datetime) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM work_item WHERE id=? AND state='leased' AND attempts=? "
            "AND lease_until=? AND lease_until>?",
            (lease.id, lease.attempts, lease.expires, _time(now)),
        ).fetchone()
        is not None
    )


def complete(
    db: sqlite3.Connection,
    lease: Lease,
    values: list[float],
    generation: Generation,
    *,
    now: datetime | None = None,
) -> bool:
    blob = vector_blob(values, generation.dimensions)
    moment = now or datetime.now(UTC)
    with snapshot(db, write=True):
        if not _owns(db, lease, moment):
            return False
        current = db.execute(
            "SELECT id,model,artifact_digest,dimensions,normalization,tokenizer_digest "
            "FROM embedding_generation WHERE id=? AND enabled=1",
            (lease.generation.id,),
        ).fetchone()
        valid = db.execute(
            "SELECT 1 FROM accepted_segment WHERE project_id=? AND revision_id=? "
            "AND known_to_seq IS NULL UNION ALL SELECT 1 FROM conflict_head "
            "WHERE project_id=? AND candidate_revision_id=? AND status='pending_review' LIMIT 1",
            (lease.project, lease.revision, lease.project, lease.revision),
        ).fetchone()
        accepted = (
            current == generation.values() and generation == lease.generation and valid is not None
        )
        if accepted:
            db.execute(
                "INSERT INTO revision_embedding VALUES (?,?,?,?,?) "
                "ON CONFLICT(revision_id,generation_id) DO UPDATE SET "
                "dimensions=excluded.dimensions,embedding=excluded.embedding",
                (lease.project, lease.revision, generation.id, generation.dimensions, blob),
            )
        db.execute("UPDATE work_item SET state='done',lease_until=NULL WHERE id=?", (lease.id,))
        return accepted


def fail(
    db: sqlite3.Connection, lease: Lease, *, now: datetime | None = None, max_attempts: int = 3
) -> bool:
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError("Invalid retry bound")
    moment = now or datetime.now(UTC)
    with snapshot(db, write=True):
        if not _owns(db, lease, moment):
            return False
        db.execute(
            "UPDATE work_item SET state=?,not_before=?,lease_until=NULL WHERE id=?",
            (
                "failed" if lease.attempts >= max_attempts else "pending",
                _time(moment + timedelta(seconds=min(300, 2 ** min(lease.attempts, 8)))),
                lease.id,
            ),
        )
        return True


def run_once(db: sqlite3.Connection, embedder: Embedder) -> bool:
    lease = claim(db)
    if lease is None:
        return False
    try:
        if lease.generation != embedder.generation:
            fail(db, lease)
            return True
        values = list(embedder.embed(lease.statement))
        complete(db, lease, values, embedder.generation)
    except Exception:
        fail(db, lease)
    return True
