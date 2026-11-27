"""Transactional writes against disposable databases and synthetic cards only."""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_contracts import record

from brain.db import initialize_database
from brain.ledger import LedgerError, record_memory


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = initialize_database(Path(self.tmp.name) / "data")
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO project VALUES ('p_test','now','Synthetic')")
        self.db.commit()

    def write(self, r):
        return record_memory(self.db, r, principal_id="agent", authorized_projects={"p_test"})

    def count(self, table):
        return self.db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def changed(self, first, kind="Decision"):
        r = record()
        r["idempotency_key"] = "next"
        r["expected_revision"] = first["data"]["revision_id"]
        r["statement"] = "Use PostgreSQL"
        r["payload"]["data"]["choice"]["value"] = "PostgreSQL"
        r["valid_from"] = "2026-11-24T10:00:00Z"
        return r

    def task(self, state="todo"):
        r = record()
        r["identity"]["entity"] = "task1"
        r["payload"] = {
            "type": "TaskState",
            "data": {"task_id": "task1", "state": state, "transition": "set"},
        }
        return r

    def test_add_replay_collision_and_support(self):
        r = record()
        original = copy.deepcopy(r)
        ack = self.write(r)
        self.assertEqual(r, original)
        self.assertEqual(ack["data"]["outcome"], "added")
        self.assertEqual(self.write(r), ack)
        r["statement"] = "different"
        with self.assertRaises(LedgerError):
            self.write(r)
        r = record()
        r["idempotency_key"] = "support"
        r["source"]["anchor"] = "another source"
        support = self.write(r)
        self.assertEqual(support["data"]["outcome"], "supported")
        self.assertEqual(self.count("memory_revision"), 1)
        self.assertEqual(self.count("source_ref"), 2)
        r["idempotency_key"] = "same-source"
        self.assertEqual(self.write(r)["data"]["outcome"], "noop")
        self.assertEqual(self.count("source_ref"), 2)
        self.assertEqual(self.count("search_doc"), 1)

    def test_conflicting_decisions_and_constraints_preserve_acceptance(self):
        for kind in ["Decision", "Constraint"]:
            r = record()
            r["identity"]["key"] = kind
            r["idempotency_key"] = kind
            if kind == "Constraint":
                r["payload"] = {
                    "type": kind,
                    "data": {
                        "operator": "eq",
                        "value": {"kind": "number", "value": 1, "unit": None},
                    },
                }
            a = self.write(r)
            r["idempotency_key"] += "2"
            r["statement"] = "Changed choice"
            r["payload"]["data"][("choice" if kind == "Decision" else "value")]["value"] = (
                "Other" if kind == "Decision" else 2
            )
            b = self.write(r)
            self.assertEqual(b["data"]["outcome"], "pending_review")
            self.assertEqual(
                self.db.execute(
                    "SELECT revision_id FROM accepted_segment WHERE memory_id=? "
                    "AND known_to_seq IS NULL",
                    (a["data"]["memory_id"],),
                ).fetchone()[0],
                a["data"]["revision_id"],
            )
        self.assertEqual(self.count("conflict_head"), 2)

    def test_expected_version_and_explicit_tech_replacement(self):
        r = record()
        r["payload"] = {"type": "TechStack", "data": {"technology": "Python", "version": "3.12"}}
        a = self.write(r)
        r["idempotency_key"] = "update"
        r["payload"]["data"]["version"] = "3.13"
        r["valid_from"] = "2026-11-24T10:00:00Z"
        r["expected_revision"] = "wrong"
        with self.assertRaises(LedgerError):
            self.write(r)
        r["expected_revision"] = a["data"]["revision_id"]
        r["supersedes_revision"] = a["data"]["revision_id"]
        b = self.write(r)
        self.assertEqual(b["data"]["outcome"], "superseded")
        old = self.db.execute(
            "SELECT valid_to FROM memory_revision WHERE id=?", (a["data"]["revision_id"],)
        ).fetchone()[0]
        self.assertIsNone(old)
        segments = self.db.execute(
            "SELECT valid_to,known_to_seq FROM accepted_segment WHERE revision_id=? ORDER BY id",
            (a["data"]["revision_id"],),
        ).fetchall()
        self.assertIsNotNone(segments[0][1])
        self.assertEqual(segments[1], ("2026-11-24T10:00:00.000000Z", None))

    def test_task_order_cas_transitions_and_reopen(self):
        r = self.task()
        a = self.write(r)
        for i, state in enumerate(["in_progress", "done", "in_progress"]):
            r["idempotency_key"] = f"task{i}"
            r["payload"]["data"]["state"] = state
            r["valid_from"] = f"2026-11-{24 + i}T10:00:00Z"
            with self.assertRaises(LedgerError):
                self.write(r)
            r["expected_revision"] = a["data"]["revision_id"]
            if i == 2:
                with self.assertRaises(LedgerError):
                    self.write(r)
                r["payload"]["data"]["transition"] = "reopen"
            a = self.write(r)
        r["idempotency_key"] = "old"
        r["valid_from"] = "2026-11-23T10:00:00Z"
        r["expected_revision"] = a["data"]["revision_id"]
        with self.assertRaises(LedgerError):
            self.write(r)

    def test_privacy_scope_and_caller_transaction(self):
        r = record()
        r["statement"] += " api_key=sk-" + "x" * 40
        self.write(r)
        self.assertNotIn("x" * 40, "\n".join(self.db.iterdump()))
        with self.assertRaises(LedgerError):
            record_memory(self.db, record(), principal_id="agent", authorized_projects=set())
        self.db.execute("INSERT INTO project VALUES ('other','now','Other')")
        with self.assertRaises(LedgerError):
            self.write(record())
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()

    def test_atomic_failure_in_projection_outbox_and_receipt(self):
        self.db.execute(
            "INSERT INTO embedding_generation VALUES ('g','synthetic','digest',384,"
            "'unit','digest',1)"
        )
        self.db.commit()
        for table in ["search_doc", "accepted_segment", "work_item", "idempotency_receipt"]:
            self.db.execute(
                f"CREATE TEMP TRIGGER fail BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
            with self.assertRaises(LedgerError):
                self.write(record())
            for authority in [
                "memory",
                "memory_revision",
                "source_ref",
                "ledger_event",
                "relation_event",
                "work_item",
            ]:
                self.assertEqual(self.count(authority), 0)
            self.db.execute("DROP TRIGGER fail")
        a = self.write(record())
        self.assertEqual(a["data"]["index_state"], "pending")
        self.assertEqual(self.count("work_item"), 1)
        self.assertEqual(
            json.loads(
                self.db.execute("SELECT response_json FROM idempotency_receipt").fetchone()[0]
            ),
            a,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE memory_revision SET statement='mutated'")

    def test_project_isolation(self):
        self.db.execute("INSERT INTO project VALUES ('other','now','Other')")
        self.db.commit()
        a = self.write(record())
        r = record()
        r["project_id"] = "other"
        b = record_memory(self.db, r, principal_id="agent", authorized_projects={"other"})
        self.assertNotEqual(a["data"]["memory_id"], b["data"]["memory_id"])

    def test_pending_support_and_scope_identity_do_not_merge(self):
        a = self.write(record())
        r = self.changed(a)
        b = self.write(r)
        r["idempotency_key"] = "support-pending"
        r["source"]["anchor"] = "independent evidence"
        c = self.write(r)
        self.assertEqual(c["data"]["revision_id"], b["data"]["revision_id"])
        self.assertEqual(c["data"]["conflict_id"], b["data"]["conflict_id"])
        self.assertEqual(self.count("conflict_head"), 1)
        self.assertEqual(self.count("accepted_segment"), 1)
        r = record()
        r["identity"]["scope"] = "other"
        r["idempotency_key"] = "other"
        self.assertEqual(self.write(r)["data"]["outcome"], "added")

    def test_unknown_validity_is_explicit_and_task_rejects_it(self):
        r = record()
        r["valid_from"] = None
        self.write(r)
        self.assertIsNone(self.db.execute("SELECT valid_from FROM accepted_segment").fetchone()[0])
        r = self.task()
        r["valid_from"] = None
        r["idempotency_key"] = "unknown-task"
        with self.assertRaises(LedgerError):
            self.write(r)
        self.assertEqual(self.count("memory_revision"), 1)

    def test_outbox_capacity_rejects_atomically(self):
        self.db.execute("INSERT INTO embedding_generation VALUES ('g','fake','d',384,'unit','d',1)")
        self.db.commit()
        with self.assertRaises(LedgerError):
            record_memory(
                self.db,
                record(),
                principal_id="agent",
                authorized_projects={"p_test"},
                max_pending_work=0,
            )
        self.assertEqual(self.count("memory_revision"), 0)
        self.assertEqual(self.count("source_ref"), 0)

    def test_failed_replacement_preserves_prior_state(self):
        r = self.task()
        a = self.write(r)
        before = list(self.db.iterdump())
        self.db.execute(
            "CREATE TEMP TRIGGER fail BEFORE INSERT ON idempotency_receipt "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        r["payload"]["data"]["state"] = "in_progress"
        r["expected_revision"] = a["data"]["revision_id"]
        r["valid_from"] = "2026-11-24T10:00:00Z"
        r["idempotency_key"] = "update"
        with self.assertRaises(LedgerError):
            self.write(r)
        self.assertEqual(list(self.db.iterdump()), before)

    def test_concurrent_replay_creates_one_assertion(self):
        from concurrent.futures import ThreadPoolExecutor

        def run(_):
            db = initialize_database(Path(self.tmp.name) / "data")
            try:
                return record_memory(
                    db, record(), principal_id="agent", authorized_projects={"p_test"}
                )
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.count("memory_revision"), 1)
