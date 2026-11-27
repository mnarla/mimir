"""One authorized discovery poll, with transactional checkpoints and reference-only work."""

import hashlib
import os
import sqlite3
from collections.abc import Iterator, Set
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from brain.budget import snapshot
from brain.ingestion import MAX_LINE_BYTES, Event, Malformed, claude, codex, parse_line
from brain.ledger import _hash
from brain.privacy import PrivacyError, SourcePolicy, scrub_text

MAX_FILE_BYTES = 1048576
MAX_BATCH_LINES = 128
LIVE_DIRECTORIES = frozenset({".codex", ".claude"})
_CHECKPOINT = (
    "SELECT file_generation,inode,byte_size,mtime_ns,committed_offset,parser_generation,"
    "sanitized_digest FROM file_discovery WHERE project_id=? AND adapter_id=? "
    "AND authorized_locator=?"
)


@dataclass(frozen=True)
class Discovery:
    status: str
    file_generation: str
    committed_offset: int
    source_refs: tuple[str, ...] = ()


def fixture_policy(root: Path, *, exclusions: tuple[str, ...] = ()) -> SourcePolicy:
    """Reject known live roots before loading their ignore rules."""
    try:
        explicit = root.expanduser()
        if explicit.is_symlink():
            raise PrivacyError("Fixture root must not be a symlink")
        canonical = explicit.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PrivacyError("Invalid fixture root") from None
    if LIVE_DIRECTORIES.intersection(canonical.parts):
        raise PrivacyError("Live transcript roots are disabled")
    return SourcePolicy(canonical, exclusions=exclusions)


def _sanitized_lines(prefix: bytes) -> Iterator[bytes]:
    for line in prefix.split(b"\n")[:-1]:
        yield scrub_text(line.decode("utf-8", errors="replace")).encode("utf-8") + b"\n"


def _descriptor(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _source_key(event: Event, project: str, adapter: str, parser: str) -> tuple[str, ...]:
    session = _hash([adapter, event.session])
    source = _hash([event.event_id])
    return project, adapter, session, source, event.digest, parser


def discover(
    db: sqlite3.Connection,
    policy: SourcePolicy,
    path: Path,
    *,
    project_id: str,
    adapter: str,
    excluded_sessions: Set[str] = frozenset(),
    max_pending_work: int = 1000,
) -> Discovery:
    """Scan one explicitly mapped fixture, never enumerate directories or extract memories."""
    if (
        adapter not in ("codex", "claude")
        or type(max_pending_work) is not int
        or not 0 <= max_pending_work <= 100000
    ):
        raise ValueError("Invalid discovery settings")
    if db.in_transaction:
        raise ValueError("Discovery requires an idle database")
    relative = policy.authorize(path)
    absolute = policy.root / relative
    if LIVE_DIRECTORIES.intersection(absolute.parts) or relative.suffix != ".jsonl":
        raise PrivacyError("Only explicit exported JSONL fixtures are allowed")
    if db.execute("SELECT 1 FROM project WHERE id=?", (project_id,)).fetchone() is None:
        raise ValueError("Project must be provisioned before discovery")
    locator = _hash(absolute.as_posix())
    key = project_id, adapter, locator
    parser = f"{adapter}-jsonl-v1"
    checkpoint_parser = parser + ":" + _hash(sorted(excluded_sessions))
    previous = db.execute(_CHECKPOINT, key).fetchone()
    with policy.open_source(relative) as source:
        before = os.fstat(source.fileno())
        if before.st_nlink != 1:
            raise PrivacyError("Fixture must not be hardlinked")
        if before.st_size > MAX_FILE_BYTES:
            raise PrivacyError("Fixture exceeds the discovery size limit")
        raw = source.read(MAX_FILE_BYTES + 1)
        after = os.fstat(source.fileno())
    if len(raw) != before.st_size or _descriptor(before) != _descriptor(after):
        raise PrivacyError("Source changed during discovery; retry")

    offset = 0
    generation = uuid4().hex
    digest = hashlib.sha256()
    if (
        previous is not None
        and previous[1] == before.st_ino
        and previous[2] <= len(raw)
        and previous[5] == checkpoint_parser
    ):
        for body in _sanitized_lines(raw[: previous[4]]):
            digest.update(body)
        if previous[6] == digest.hexdigest():
            generation, offset = previous[0], previous[4]
        else:
            digest = hashlib.sha256()
    session = ""
    if adapter == "codex" and raw:
        header = raw.find(b"\n")
        if header < 0:
            status = "malformed" if len(raw) >= MAX_LINE_BYTES else "partial"
            return Discovery(status, generation, offset)
        try:
            session = codex.session_header(parse_line(raw[: header + 1]))
        except Malformed:
            return Discovery("malformed", generation, offset)

    parsed: list[tuple[int, Event | None]] = []
    position = offset
    status = "complete"
    while position < len(raw):
        if len(parsed) >= MAX_BATCH_LINES:
            status = "batch_limit"
            break
        end = raw.find(b"\n", position, position + MAX_LINE_BYTES)
        if end < 0:
            status = "malformed" if len(raw) - position >= MAX_LINE_BYTES else "partial"
            break
        try:
            line = parse_line(raw[position : end + 1])
            candidate = (
                codex.message(line, session, position, excluded_sessions)
                if adapter == "codex"
                else claude.message(line, excluded_sessions)
            )
        except Malformed:
            status = "malformed"
            break
        position = end + 1
        parsed.append((position, candidate))

    fingerprints = {offset: digest.hexdigest()}
    start = offset
    for next_offset, _ in parsed:
        for body in _sanitized_lines(raw[start:next_offset]):
            digest.update(body)
        fingerprints[next_offset] = digest.hexdigest()
        start = next_offset

    refs = []
    with snapshot(db, write=True):
        if db.execute(_CHECKPOINT, key).fetchone() != previous:
            raise ValueError("Discovery checkpoint changed; retry")
        pending = db.execute(
            "SELECT count(*) FROM work_item WHERE state IN ('pending','leased')"
        ).fetchone()[0]
        now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        for next_offset, candidate in parsed:
            if candidate is not None:
                source_key = _source_key(candidate, project_id, adapter, parser)
                exists = db.execute(
                    "SELECT 1 FROM source_ref WHERE project_id=? AND source_tool=? "
                    "AND session_id=? AND source_id=? AND source_revision=? "
                    "AND parser_generation=?",
                    source_key,
                ).fetchone()
                if exists is None:
                    if pending >= max_pending_work:
                        status = "queue_full"
                        break
                    ref = uuid4().hex
                    db.execute(
                        "INSERT INTO source_ref(id,project_id,source_tool,source_agent,session_id,"
                        "source_timestamp,anchor,locator,source_id,source_revision,sanitized_digest,"
                        "parser_generation) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?)",
                        (
                            ref,
                            project_id,
                            adapter,
                            source_key[2],
                            candidate.timestamp,
                            f"{candidate.role} at byte {offset}",
                            locator,
                            *source_key[3:5],
                            candidate.digest,
                            parser,
                        ),
                    )
                    db.execute(
                        "INSERT INTO work_item(project_id,kind,source_ref_id,dedup_key,state,"
                        "not_before) VALUES (?,'needs_agent',?,?,'pending',?)",
                        (project_id, ref, _hash(["source_discovery", ref]), now),
                    )
                    refs.append(ref)
                    pending += 1
            offset = next_offset
        waiting = db.execute(
            "SELECT 1 FROM work_item w JOIN source_ref r ON r.id=w.source_ref_id "
            "WHERE r.project_id=? AND r.source_tool=? AND r.locator=? "
            "AND w.kind='needs_agent' AND w.state IN ('pending','leased') LIMIT 1",
            key,
        ).fetchone()
        db.execute(
            "INSERT INTO file_discovery VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id,adapter_id,authorized_locator) DO UPDATE SET "
            "file_generation=excluded.file_generation,inode=excluded.inode,"
            "byte_size=excluded.byte_size,mtime_ns=excluded.mtime_ns,"
            "committed_offset=excluded.committed_offset,parser_generation=excluded.parser_generation,"
            "sanitized_digest=excluded.sanitized_digest,extraction_state=excluded.extraction_state",
            (
                *key,
                generation,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                offset,
                checkpoint_parser,
                fingerprints[offset],
                "needs_agent" if waiting else "discovered",
            ),
        )
    return Discovery(status, generation, offset, tuple(refs))
