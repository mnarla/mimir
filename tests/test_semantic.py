"""Model-free semantic indexing and retrieval using deterministic vectors."""

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_ledger
from test_contracts import record

from mimir.config import ConfigError, load_config
from mimir.embeddings import EmbeddingError, Generation, LocalEmbedder, load_vec, vector_blob
from mimir.ledger import _hash, record_memory
from mimir.retrieval import search_memory
from mimir.worker import backfill, claim, complete, fail, register_generation, run_once


class SyntheticEmbedder:
    generation = Generation("g", "synthetic", "a" * 64, 3, "unit", "b" * 64)

    def embed(self, text, *, query=False):
        return [1.0, 0.0, 0.0]


class SemanticTests(unittest.TestCase):
    setUp = test_ledger.LedgerTests.setUp
    write = test_ledger.LedgerTests.write

    def test_native_cosine_filters_projects_and_has_stable_ties(self):
        self.enable()
        if not load_vec(self.db):
            self.skipTest("sqlite-vec extra unavailable")
        first = self.write(record())["data"]
        self.db.execute("INSERT INTO project VALUES ('other','now','Synthetic other')")
        self.db.commit()
        other = record()
        other["project_id"] = "other"
        record_memory(self.db, other, principal_id="agent", authorized_projects={"other"})
        while run_once(self.db, SyntheticEmbedder()):
            pass
        result = search_memory(
            self.db, self.request(), authorized_projects={"p_test"}, embedder=SyntheticEmbedder()
        )["data"]
        again = search_memory(
            self.db, self.request(), authorized_projects={"p_test"}, embedder=SyntheticEmbedder()
        )["data"]
        self.assertEqual(result, again)
        self.assertEqual([item["revision_id"] for item in result["items"]], [first["revision_id"]])

    def test_generation_changes_during_query_fall_back(self):
        self.enable()
        self.write(record())
        if not load_vec(self.db):
            self.skipTest("sqlite-vec extra unavailable")
        database = self.db

        class SwitchingEmbedder(SyntheticEmbedder):
            def embed(self, text, *, query=False):
                database.execute("UPDATE embedding_generation SET enabled=0")
                database.commit()
                return [1, 0, 0]

        result = search_memory(
            self.db,
            self.request("SQLite"),
            authorized_projects={"p_test"},
            embedder=SwitchingEmbedder(),
        )["data"]
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("semantic_unavailable", result["diagnostics"])

    def test_generation_registration_and_bounded_backfill(self):
        self.write(record())
        generation = SyntheticEmbedder.generation
        register_generation(self.db, generation)
        register_generation(self.db, generation)
        self.assertEqual(backfill(self.db, generation.id, limit=1), 1)
        self.assertEqual(backfill(self.db, generation.id, limit=1), 0)
        changed = Generation("g", "different", "a" * 64, 3, "unit", "b" * 64)
        with self.assertRaises(EmbeddingError):
            register_generation(self.db, changed)
        run_once(self.db, SyntheticEmbedder())
        self.assertEqual(backfill(self.db, generation.id), 0)

    def test_changed_generation_and_dimension_mismatch(self):
        generation = self.enable()
        self.write(record())
        lease = claim(self.db)
        with self.assertRaises(EmbeddingError):
            complete(self.db, lease, [1, 0], generation)
        self.db.execute(
            "UPDATE embedding_generation SET artifact_digest=? WHERE id='g'", ("c" * 64,)
        )
        self.db.commit()
        self.assertFalse(complete(self.db, lease, [1, 0, 0], generation))

    def test_crashed_jobs_have_bounded_attempts(self):
        self.enable()
        self.write(record())
        now = datetime.now(UTC)
        for index in range(3):
            self.assertIsNotNone(
                claim(self.db, now=now + timedelta(seconds=index * 2), lease_seconds=1)
            )
        self.assertIsNone(claim(self.db, now=now + timedelta(seconds=8)))
        self.assertEqual(self.db.execute("SELECT state FROM work_item").fetchone()[0], "failed")

    def test_local_session_is_lazy_verified_and_shared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = [
                "model_optimized.onnx",
                "tokenizer.json",
                "config.json",
                "tokenizer_config.json",
                "special_tokens_map.json",
            ]
            manifest = {}
            for name in names:
                (root / name).write_bytes(b"synthetic fixture")
                manifest[name] = hashlib.sha256(b"synthetic fixture").hexdigest()
            generation = Generation(
                "offline",
                "BAAI/bge-small-en-v1.5",
                _hash(manifest),
                384,
                "unit",
                manifest["tokenizer.json"],
            )
            calls = []

            def factory(**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    embed=lambda *args, **kw: iter([[1.0] + [0.0] * 383]),
                    query_embed=lambda *args, **kw: iter([[1.0] + [0.0] * 383]),
                )

            with (
                patch.object(LocalEmbedder, "_session", None),
                patch.object(LocalEmbedder, "_session_key", None),
            ):
                first = LocalEmbedder(generation, root, manifest)
                second = LocalEmbedder(generation, root, manifest)
                self.assertEqual(calls, [])
                with patch(
                    "mimir.embeddings.importlib.import_module",
                    return_value=SimpleNamespace(TextEmbedding=factory),
                ):
                    first.embed("document")
                    second.embed("query", query=True)
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0]["local_files_only"])
                self.assertEqual(calls[0]["specific_model_path"], str(root))
            (root / "tokenizer.json").write_bytes(b"changed")
            with self.assertRaises(EmbeddingError):
                first._verify()

    def test_semantic_configuration_is_explicit_and_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "mimir.toml"
            config.write_text('[semantic]\ngeneration_id="g"\nmodel_dir="models"\n')
            settings = load_config(config)
            self.assertEqual(settings.semantic_model_dir, root / "models")
            config.write_text('[semantic]\ngeneration_id="g"\n')
            with self.assertRaises(ConfigError):
                load_config(config)
            config.write_text("[semantic]\nenabled=true\n")
            with self.assertRaises(ConfigError):
                load_config(config)

    def enable(self):
        generation = SyntheticEmbedder.generation
        self.db.execute(
            "INSERT INTO embedding_generation VALUES (?,?,?,?,?,?,1)", generation.values()
        )
        self.db.commit()
        return generation

    def request(self, query="absent"):
        return dict(
            scope=dict(
                project_id="p_test",
                additional_projects=[],
                valid_at="2026-11-25T00:00:00Z",
                known_seq=None,
                token_budget=16384,
                tokenizer="utf8_bytes",
            ),
            query=query,
            exact_key=None,
            types=[],
            limit=50,
        )

    def test_vector_validation(self):
        for values in ([0, 0, 0], [1, 2], [float("nan"), 0, 1], [True, 0, 1], [1e100, 0, 1]):
            with self.assertRaises(EmbeddingError):
                vector_blob(values, 3)
        self.assertEqual(len(vector_blob([1, 0, 0], 3)), 12)

    def test_worker_completion_and_hybrid_search(self):
        self.enable()
        ack = self.write(record())["data"]
        self.assertTrue(run_once(self.db, SyntheticEmbedder()))
        self.assertFalse(run_once(self.db, SyntheticEmbedder()))
        self.assertEqual(self.db.execute("SELECT state FROM work_item").fetchone()[0], "done")
        supported = record()
        supported["idempotency_key"] = "support-ready"
        supported["source"]["anchor"] = "new evidence"
        self.assertEqual(self.write(supported)["data"]["index_state"], "ready")
        available = load_vec(self.db)
        result = search_memory(
            self.db, self.request(), authorized_projects={"p_test"}, embedder=SyntheticEmbedder()
        )["data"]
        if available:
            self.assertEqual(result["items"][0]["revision_id"], ack["revision_id"])
            self.assertEqual(result["embedding_generation"], "g")
            self.assertNotIn("semantic_unavailable", result["diagnostics"])
        else:
            self.assertIn("semantic_unavailable", result["diagnostics"])

    def test_expired_lease_cannot_overwrite_new_owner(self):
        generation = self.enable()
        self.write(record())
        now = datetime.now(UTC)
        first = claim(self.db, now=now, lease_seconds=1)
        second = claim(self.db, now=now + timedelta(seconds=2))
        self.assertIsNotNone(second)
        self.assertFalse(
            complete(self.db, first, [1, 0, 0], generation, now=now + timedelta(seconds=2))
        )
        self.assertTrue(
            complete(self.db, second, [1, 0, 0], generation, now=now + timedelta(seconds=3))
        )

    def test_disabled_or_changed_generation_discards_result(self):
        generation = self.enable()
        self.write(record())
        lease = claim(self.db)
        self.db.execute("UPDATE embedding_generation SET enabled=0 WHERE id='g'")
        self.db.commit()
        self.assertFalse(complete(self.db, lease, [1, 0, 0], generation))
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM revision_embedding").fetchone()[0], 0
        )
        register_generation(self.db, generation)
        self.assertEqual(backfill(self.db, generation.id), 1)

    def test_retry_backoff_and_failure_do_not_rollback_fact(self):
        self.enable()
        self.write(record())
        now = datetime.now(UTC)
        lease = claim(self.db, now=now)
        fail(self.db, lease, now=now, max_attempts=2)
        self.assertIsNone(claim(self.db, now=now))
        lease = claim(self.db, now=now + timedelta(seconds=60))
        fail(self.db, lease, now=now + timedelta(seconds=60), max_attempts=2)
        self.assertEqual(self.db.execute("SELECT state FROM work_item").fetchone()[0], "failed")
        self.assertEqual(self.db.execute("SELECT count(*) FROM memory_revision").fetchone()[0], 1)

    def test_inference_occurs_outside_transaction(self):
        self.enable()
        self.write(record())
        database = self.db

        class Probe(SyntheticEmbedder):
            def embed(self, text, *, query=False):
                if database.in_transaction:
                    raise AssertionError("inference in transaction")
                return super().embed(text, query=query)

        run_once(self.db, Probe())
        search_memory(self.db, self.request(), authorized_projects={"p_test"}, embedder=Probe())

    def test_partial_vectors_and_eligible_set_before_cosine(self):
        self.enable()
        first = self.write(record())["data"]
        run_once(self.db, SyntheticEmbedder())
        other = record()
        other["idempotency_key"] = "other"
        other["identity"]["key"] = "other"
        self.write(other)
        if not load_vec(self.db):
            self.skipTest("sqlite-vec extra unavailable")
        result = search_memory(
            self.db,
            self.request("SQLite"),
            authorized_projects={"p_test"},
            embedder=SyntheticEmbedder(),
        )["data"]
        self.assertIn("semantic_partial", result["diagnostics"])
        self.assertIn("index_lag", result["diagnostics"])
        self.assertIn(first["revision_id"], [item["revision_id"] for item in result["items"]])
        request = self.request()
        request["scope"]["known_seq"] = 0
        self.assertEqual(
            search_memory(
                self.db, request, authorized_projects={"p_test"}, embedder=SyntheticEmbedder()
            )["data"]["items"],
            [],
        )
