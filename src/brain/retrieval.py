"""Snapshot-scoped lexical/exact retrieval and whole-item context packing."""

import re
import sqlite3
from collections.abc import Set
from datetime import UTC, datetime
from typing import Any

from brain.budget import (
    SOURCE_COLUMNS,
    ReadError,
    TokenCounter,
    authorize,
    canonical,
    metadata,
    recount,
    snapshot,
    source_from,
)
from brain.contracts import prepare_input, validate_output

TYPE_ORDER = ("Decision", "Constraint", "TaskState", "TechStack", "BugFix")


def search_memory(
    db: sqlite3.Connection,
    payload: object,
    *,
    authorized_projects: Set[str],
    counter: TokenCounter | None = None,
) -> dict[str, Any]:
    return _read(db, "search_memory", payload, authorized_projects, counter)


def get_project_context(
    db: sqlite3.Connection,
    payload: object,
    *,
    authorized_projects: Set[str],
    counter: TokenCounter | None = None,
) -> dict[str, Any]:
    return _read(db, "get_project_context", payload, authorized_projects, counter)


def _read(
    db: sqlite3.Connection,
    name: str,
    payload: object,
    allowed: Set[str],
    counter: TokenCounter | None,
) -> dict[str, Any]:
    request = prepare_input(name, payload, configured_tokenizer=counter is not None)
    scope = request["scope"]
    counter = counter if scope["tokenizer"] == "configured" else None
    projects = [scope["project_id"], *scope["additional_projects"]]
    authorize(projects, allowed)
    with snapshot(db):
        upper = db.execute("SELECT coalesce(max(seq),0) FROM ledger_event").fetchone()[0]
        known = upper if scope["known_seq"] is None else scope["known_seq"]
        if known > upper:
            raise ReadError("invalid_input")
        valid = scope["valid_at"] or datetime.now(UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        marks = ",".join("?" for _ in projects)
        base = (
            f"FROM accepted_segment a JOIN memory_revision r ON r.id=a.revision_id "
            f"JOIN memory m ON m.id=a.memory_id JOIN source_ref s ON s.id=r.source_ref_id "
            f"WHERE a.project_id IN ({marks}) AND a.known_from_seq<=? "
            "AND (a.known_to_seq IS NULL OR ?<a.known_to_seq) "
        )
        args = [*projects, known, known]
        unknown = db.execute(
            "SELECT 1 FROM accepted_segment WHERE project_id IN (SELECT value FROM json_each(?)) "
            "AND known_from_seq<=? AND (known_to_seq IS NULL OR ?<known_to_seq) "
            "AND valid_from IS NULL LIMIT 1",
            (canonical(projects).decode("utf-8"), known, known),
        ).fetchone()
        rows = db.execute(
            "SELECT DISTINCT m.id,r.id,r.version,m.project_id,m.kind,m.entity_key,m.fact_key,"
            "m.applicability_scope,r.statement,a.valid_from,a.valid_to,r.recorded_at,"
            f"{SOURCE_COLUMNS},m.cardinality,a.known_from_seq "
            + base
            + "AND a.valid_from IS NOT NULL AND a.valid_from<=? "
            "AND (a.valid_to IS NULL OR ?<a.valid_to) LIMIT 10001",
            [*args, valid, valid],
        ).fetchall()
        if len(rows) > 10000:
            raise ReadError("resource_limit")
        identities: dict[tuple[Any, ...], str] = {}
        for row in rows:
            key = (row[3], row[4], row[5], row[6], row[7])
            if row[18] == "single" and key in identities and identities[key] != row[1]:
                raise ReadError("unavailable")
            identities[key] = row[1]
        conflicts = db.execute(
            "SELECT c.id,c.project_id,c.memory_id,c.candidate_revision_id FROM conflict_head c "
            "WHERE c.project_id IN (SELECT value FROM json_each(?)) "
            "AND c.opened_seq<=? AND NOT EXISTS "
            "(SELECT 1 FROM ledger_event e WHERE e.conflict_id=c.id AND e.event='conflict_resolve' "
            "AND e.seq<=?) ORDER BY c.project_id,c.id",
            (canonical(projects).decode("utf-8"), known, known),
        ).fetchall()
        warnings = [
            dict(
                conflict_id=row[0],
                project_id=row[1],
                memory_id=row[2],
                candidate_revision=row[3],
                code="pending_review",
            )
            for row in conflicts
        ]
        blocked = {row[2] for row in conflicts}
        rows = [row for row in rows if row[0] not in blocked]
        types = request.get("types", [])
        rows = [row for row in rows if not types or row[4] in types]
        query = request.get("query", request.get("goal"))
        exact = request.get("exact_key")
        exact_rows = [
            row
            for row in rows
            if exact and (row[5], row[6], row[7]) == (exact["entity"], exact["key"], exact["scope"])
        ]
        terms = re.findall(r"\w+", query or "", re.UNICODE)
        lexical: list[str] = []
        if terms and rows:
            eligible = sorted({row[1] for row in rows})
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            hits = db.execute(
                "SELECT d.revision_id FROM search_fts JOIN search_doc d "
                "ON d.id=search_fts.rowid JOIN json_each(?) eligible "
                "ON eligible.value=d.revision_id WHERE search_fts MATCH ? "
                "ORDER BY bm25(search_fts),d.revision_id LIMIT 129",
                (canonical(eligible).decode("utf-8"), expression),
            ).fetchall()
            lexical = [row[0] for row in hits]
        scores: dict[str, float] = {}
        capped = len(exact_rows) > 128 or len(lexical) > 128
        exact_rows.sort(key=lambda row: row[1])
        exact_ids = {row[1] for row in exact_rows[:128]}
        for weight, channel in (
            (2, [row[1] for row in sorted(exact_rows, key=lambda row: row[1])][:128]),
            (1, lexical[:128]),
        ):
            for rank, revision in enumerate(channel, 1):
                scores[revision] = scores.get(revision, 0) + weight / (60 + rank)
        if query is not None:
            rows = [row for row in rows if row[1] in scores]
            rows.sort(
                key=lambda row: (
                    row[1] not in exact_ids,
                    -scores[row[1]],
                    row[3] != projects[0],
                    TYPE_ORDER.index(row[4]),
                    row[0],
                    row[1],
                )
            )
        else:
            rows.sort(
                key=lambda row: (
                    TYPE_ORDER.index(row[4]),
                    row[3] != projects[0],
                    -row[19],
                    row[0],
                    row[1],
                )
            )
        diagnostics = ["semantic_unavailable"]
        if unknown:
            diagnostics.append("unknown_validity_excluded")
        if capped:
            diagnostics.append("candidate_limit")
        data = dict(
            snapshot_seq=known,
            effective_valid_at=valid,
            items=[],
            conflicts=[],
            conflicts_omitted=len(warnings),
            diagnostics=diagnostics,
            budget=metadata(scope["token_budget"], counter),
            embedding_generation=None,
            ranking_version="lexical-v1",
        )

        def warning_state() -> None:
            data["conflicts_omitted"] = len(warnings) - len(data["conflicts"])
            if data["conflicts_omitted"] and "more_conflicts" not in diagnostics:
                diagnostics.append("more_conflicts")
            if not data["conflicts_omitted"] and "more_conflicts" in diagnostics:
                diagnostics.remove("more_conflicts")
            data["budget"]["truncated"] = bool(data["conflicts_omitted"] or capped)

        warning_state()
        if not recount(data, counter):
            raise ReadError("budget_too_small")
        for warning in warnings[:16]:
            data["conflicts"].append(warning)
            warning_state()
            if not recount(data, counter):
                data["conflicts"].pop()
                warning_state()
                break
        limit = request.get("limit", 50)
        for row in rows:
            item = dict(
                memory_id=row[0],
                revision_id=row[1],
                version=row[2],
                project_id=row[3],
                type=row[4],
                identity=dict(entity=row[5], key=row[6], scope=row[7]),
                statement=row[8],
                valid_from=row[9],
                valid_to=row[10],
                recorded_at=row[11],
                source=source_from(row[12:18]),
                badge=f"{row[12]} · v{row[2]}",
                score=scores.get(row[1], 0),
                pending_conflict=False,
            )
            data["items"].append(item)
            if len(data["items"]) > limit or not recount(data, counter):
                data["items"].pop()
                data["budget"]["truncated"] = True
        while not recount(data, counter):
            if data["items"]:
                data["items"].pop()
            else:
                raise ReadError("budget_too_small")
        result = dict(ok=True, data=data)
        validate_output(name, result)
        return result
