"""Run the same production search pipeline with three candidate-channel setups."""

import hashlib
import math
import re
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from sqlite3 import Connection
from time import perf_counter
from typing import Any

from benchmarks.fixture import AS_OF, decision, search
from benchmarks.handoff import latency
from brain.db import open_database
from brain.embeddings import Generation, load_vec
from brain.ledger import record_memory
from brain.retrieval import search_memory
from brain.worker import register_generation, run_once

# Gold statements and independently phrased queries. One relevant card per query.
CASES = (
    (
        "receipts",
        "Keep idempotency receipts for 30 days after a write.",
        "idempotency receipts retention",
        "how long can a repeat submission reuse its acknowledgment",
    ),
    (
        "leases",
        "Lease embedding jobs for 60 seconds before allowing another worker to claim them.",
        "embedding lease duration",
        "when can another process reclaim a stalled indexing job",
    ),
    (
        "unicode",
        "Normalize display labels to NFC before validating their length.",
        "NFC display labels",
        "which Unicode form is used to count title characters",
    ),
    (
        "timeouts",
        "Set the import HTTP deadline to 4 seconds and cancel the request on expiry.",
        "import HTTP deadline",
        "how soon does a stuck upload request get cancelled",
    ),
    (
        "snapshots",
        "Write compressed SQLite backup snapshots to the private export directory.",
        "compressed SQLite backup snapshots",
        "where should local database recovery copies go",
    ),
    (
        "journals",
        "Run a passive WAL checkpoint after each queued import batch.",
        "passive WAL checkpoint",
        "when should the write-ahead log be checkpointed",
    ),
    (
        "timestamps",
        "Encode source timestamps as UTC with six fractional digits.",
        "UTC source timestamps precision",
        "how many decimal places are in a recorded source time",
    ),
    (
        "bundles",
        "Verify the SHA-256 digest before loading a local fixture bundle.",
        "SHA-256 fixture bundle verification",
        "what integrity check runs before opening test data",
    ),
    (
        "pagination",
        "Use a sequence cursor for history pages and preserve forward event ordering.",
        "sequence cursor for history pagination",
        "how are ledger pages resumed in chronological order",
    ),
    (
        "scopes",
        "Require a separate review grant before accepting a pending decision.",
        "review grant for pending decision",
        "who can approve a proposed change to a disputed choice",
    ),
    (
        "redaction",
        "Scrub secret assignments from provenance anchors before storing records.",
        "scrub provenance anchors",
        "where are credentials removed from a source citation",
    ),
    (
        "retries",
        "Retry transient indexing failures with exponential backoff.",
        "exponential backoff for indexing failures",
        "how should retry delays grow after indexing errors",
    ),
)

DISTRACTORS = (
    "Use monotonic intervals for queue metrics and backlog reporting.",
    "Include fixture version metadata in the archive header.",
    "Place temporary files on the same volume as their final destination.",
    "Log bounded diagnostic codes when import parsing stops.",
    "Reject unrecognized options in the timing configuration.",
    "Document administrative commands in the internal runbook.",
)


class SyntheticEmbedder:
    """Hash word and character features without access to the relevance labels."""

    generation = Generation(
        "synthetic-features-v1",
        "hashed-words-and-trigrams",
        hashlib.sha256(b"sha256-unsigned-words-weight2-trigrams-weight1-v1").hexdigest(),
        384,
        "unit",
        hashlib.sha256(b"unicode-word-casefold-v1").hexdigest(),
    )

    def embed(self, text: str, *, query: bool = False) -> list[float]:
        values = [0.0] * self.generation.dimensions

        def add(feature: str, weight: float) -> None:
            digest = hashlib.sha256(feature.encode()).digest()
            bucket = int.from_bytes(digest[:4], "big") % len(values)
            values[bucket] += weight

        for word in re.findall(r"\w+", text.casefold()):
            add(f"word:{word}", 2.0)
            padded = f" {word} "
            for index in range(len(padded) - 2):
                add(padded[index : index + 3], 1.0)
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]


def seed(db: Connection, project: str, unrelated: str) -> tuple[dict[str, str], set[str], int]:
    def insert(key: str, statement: str, target: str = project, **time: str) -> str:
        result = record_memory(
            db,
            decision(target, key, statement, choice=key, **time),
            principal_id="benchmark-fixture",
            authorized_projects={target},
        )
        assert result["data"]["outcome"] == "added"
        return str(result["data"]["revision_id"])

    gold = {key: insert(key, statement) for key, statement, _, _ in CASES}
    eligible = set(gold.values())
    for key, _, keywords, _ in CASES:
        for index, statement in enumerate(DISTRACTORS):
            eligible.add(insert(f"decoy-{key}-{index}", f"{statement} Reference: {keywords}."))
    for key, statement, _, _ in CASES:
        insert(f"other-{key}", statement, unrelated)
        insert(f"future-{key}", statement, effective="2027-01-01T00:00:00Z")
        insert(f"expired-{key}", statement, expires="2026-02-01T00:00:00Z")
    known: int = db.execute("SELECT max(seq) FROM ledger_event").fetchone()[0]
    for key, statement, _, _ in CASES:
        insert(f"late-{key}", statement)
    return gold, eligible, known


def compare(data_dir: Path, project: str, unrelated: str, repeats: int) -> dict[str, Any]:
    embedder = SyntheticEmbedder()
    with (
        closing(open_database(data_dir)) as db,
        closing(open_database(data_dir)) as vector_only,
    ):
        if not load_vec(db):
            raise RuntimeError("Native sqlite-vec 0.1.9 required. Use --demo-only for lexical MCP.")
        # A connection-local empty FTS5 table disables only the lexical candidate stream.
        # Snapshot filtering, native cosine, fusion, and packing still use production code.
        vector_only.execute(
            "CREATE VIRTUAL TABLE temp.search_fts USING fts5(statement, identity_text)"
        )
        register_generation(db, embedder.generation)
        started = perf_counter()
        gold, eligible, known = seed(db, project, unrelated)
        seed_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        while run_once(db, embedder):
            pass
        index_ms = (perf_counter() - started) * 1000
        revisions = db.execute("SELECT count(*) FROM memory_revision").fetchone()[0]
        vectors = db.execute("SELECT count(*) FROM revision_embedding").fetchone()[0]
        assert vectors == revisions - 2, "Expected one vector per benchmark revision"
        configs = {"fts": (db, None), "vector": (vector_only, embedder), "hybrid": (db, embedder)}
        modes = list(configs)
        timings: dict[str, list[float]] = defaultdict(list)
        first: dict[str, float] = {}
        ranks: dict[str, dict[tuple[str, str], int | None]] = defaultdict(dict)
        violations: dict[str, int] = defaultdict(int)
        queries = [
            (key, kind, search(project, prompt, known=known))
            for key, _, keywords, paraphrase in CASES
            for kind, prompt in (("keyword", keywords), ("paraphrase", paraphrase))
        ]

        for repetition in range(repeats + 1):
            for index, (key, kind, request) in enumerate(queries):
                offset = (repetition + index) % len(modes)
                for mode in modes[offset:] + modes[:offset]:
                    conn, semantic = configs[mode]
                    started = perf_counter()
                    result = search_memory(
                        conn,
                        request,
                        authorized_projects={project},
                        embedder=semantic,
                    )["data"]
                    elapsed = perf_counter() - started
                    first.setdefault(mode, elapsed * 1000)
                    if repetition:
                        timings[mode].append(elapsed)
                    if semantic:
                        assert result["embedding_generation"] == embedder.generation.id
                        assert not {"semantic_unavailable", "semantic_partial"} & set(
                            result["diagnostics"]
                        )
                    else:
                        assert result["embedding_generation"] is None
                    hits = [item["revision_id"] for item in result["items"]]
                    if mode == "vector":
                        assert all(
                            item["score"] == 1 / (60 + rank)
                            for rank, item in enumerate(result["items"], 1)
                        ), "Vector control contains non-vector ranking contributions"
                    violations[mode] += sum(hit not in eligible for hit in hits)
                    rank = hits.index(gold[key]) + 1 if gold[key] in hits else None
                    if repetition:
                        assert rank == ranks[mode][key, kind]
                    else:
                        ranks[mode][key, kind] = rank

    def recall(found: list[int | None], k: int) -> float:
        return round(sum(rank is not None and rank <= k for rank in found) / len(found), 4)

    return {
        "queries": len(queries),
        "eligible_revisions": len(eligible),
        "total_revisions_including_handoff": revisions,
        "vectors": vectors,
        "embedding": embedder.generation.model,
        "dimensions": embedder.generation.dimensions,
        "sqlite_vec": "0.1.9",
        "valid_at": AS_OF,
        "known_seq": known,
        "limit": 5,
        "token_budget_utf8_bytes": 16384,
        "seed_ms": round(seed_ms, 3),
        "synthetic_index_ms": round(index_ms, 3),
        "modes": {
            mode: {
                **latency(timings[mode]),
                "first_query_ms": round(first[mode], 3),
                "recall_at_1": recall(list(ranks[mode].values()), 1),
                "recall_at_5": recall(list(ranks[mode].values()), 5),
                "recall_at_5_by_query_type": {
                    kind: recall(
                        [rank for (_, group), rank in ranks[mode].items() if group == kind], 5
                    )
                    for kind in ("keyword", "paraphrase")
                },
                "scope_violations": violations[mode],
            }
            for mode in modes
        },
    }
