"""Synthetic read/review acceptance cases over disposable ledgers."""

import json
import unittest

import test_ledger
from test_contracts import record

from brain.budget import ReadError, TokenCounter, canonical
from brain.history import get_history
from brain.retrieval import get_project_context, search_memory
from brain.review import resolve_conflict


class ReadTests(unittest.TestCase):
    setUp = test_ledger.LedgerTests.setUp
    write = test_ledger.LedgerTests.write
    changed = test_ledger.LedgerTests.changed

    def test_warning_cap_and_budget_omission_counts(self):
        for index in range(18):
            first = record()
            first["identity"]["key"] = f"key{index}"
            first["idempotency_key"] = f"first{index}"
            ack = self.write(first)
            second = self.changed(ack)
            second["identity"]["key"] = first["identity"]["key"]
            second["idempotency_key"] = f"second{index}"
            self.write(second)
        result = self.search()
        self.assertEqual(len(result["conflicts"]), 16)
        self.assertEqual(result["conflicts_omitted"], 2)
        self.assertIn("more_conflicts", result["diagnostics"])
        small = self.search(scope=self.scope(token_budget=512))
        self.assertEqual(len(small["conflicts"]) + small["conflicts_omitted"], 18)
        self.assertLessEqual(len(canonical(small)), 512)

    def test_budget_skips_large_item_and_keeps_smaller_item(self):
        first = record()
        first["statement"] = "SQLite " + "x" * 1900
        self.write(first)
        second = record()
        second["identity"]["key"] = "second"
        second["idempotency_key"] = "second"
        ack = self.write(second)
        result = self.search(scope=self.scope(token_budget=1400))
        self.assertEqual(
            [item["revision_id"] for item in result["items"]], [ack["data"]["revision_id"]]
        )
        self.assertTrue(result["budget"]["truncated"])
        self.assertEqual(len(canonical(result)), result["budget"]["used"])

    def test_new_evidence_invalidates_review_version(self):
        ack = self.write(record())
        candidate = self.changed(ack)
        conflict = self.write(candidate)["data"]["conflict_id"]
        request = self.resolution(conflict)
        candidate["idempotency_key"] = "independent-support"
        candidate["source"]["anchor"] = "new evidence"
        self.write(candidate)
        with self.assertRaises(ReadError):
            resolve_conflict(self.db, request, principal_id="review", review_projects={"p_test"})

    def test_filtering_precedes_candidate_cap_and_small_items_survive(self):
        for index in range(132):
            request = record()
            request["idempotency_key"] = f"future{index}"
            request["identity"]["key"] = f"future{index}"
            request["valid_from"] = "2027-01-01T00:00:00Z"
            self.write(request)
        ack = self.write(record())
        result = self.search(limit=1)
        self.assertEqual(result["items"][0]["revision_id"], ack["data"]["revision_id"])
        self.assertNotIn("candidate_limit", result["diagnostics"])

    def test_set_coexist_and_review_rollback(self):
        first = record()
        first["cardinality"] = "set"
        ack = self.write(first)
        candidate = self.changed(ack)
        candidate["cardinality"] = "set"
        conflict = self.write(candidate)["data"]["conflict_id"]
        request = self.resolution(conflict, action="coexist")
        before = list(self.db.iterdump())
        self.db.execute(
            "CREATE TEMP TRIGGER fail BEFORE INSERT ON idempotency_receipt "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(ReadError):
            resolve_conflict(self.db, request, principal_id="review", review_projects={"p_test"})
        self.assertEqual(before, list(self.db.iterdump()))
        self.db.execute("DROP TRIGGER fail")
        resolve_conflict(self.db, request, principal_id="review", review_projects={"p_test"})
        self.assertEqual(len(self.search(query="SQLite PostgreSQL")["items"]), 2)

    def test_configured_counter_and_safe_invariant_failure(self):
        ack = self.write(record())["data"]
        counter = TokenCounter("synthetic-byte-counter-v1", len)
        request = dict(
            scope=self.scope(tokenizer="configured"),
            query="SQLite",
            exact_key=None,
            types=[],
            limit=1,
        )
        result = search_memory(self.db, request, authorized_projects={"p_test"}, counter=counter)[
            "data"
        ]
        self.assertEqual(result["budget"]["counting"], "exact")
        self.assertEqual(result["budget"]["used"], len(canonical(result)))
        self.db.execute(
            "INSERT INTO accepted_segment(project_id,memory_id,revision_id,valid_from,"
            "known_from_seq) SELECT project_id,memory_id,revision_id,valid_from,"
            "known_from_seq FROM accepted_segment"
        )
        self.db.commit()
        self.assertEqual(len(self.search()["items"]), 1)
        candidate = self.changed(dict(data=ack))
        candidate_ack = self.write(candidate)["data"]
        self.db.execute(
            "INSERT INTO accepted_segment(project_id,memory_id,revision_id,valid_from,"
            "known_from_seq) VALUES (?,?,?,?,?)",
            (
                "p_test",
                ack["memory_id"],
                candidate_ack["revision_id"],
                "2026-11-24T00:00:00.000000Z",
                candidate_ack["ledger_seq"],
            ),
        )
        self.db.commit()
        with self.assertRaises(ReadError):
            self.search()

    def scope(self, **changes):
        result = dict(
            project_id="p_test",
            additional_projects=[],
            valid_at="2026-11-25T00:00:00Z",
            known_seq=None,
            token_budget=16384,
            tokenizer="utf8_bytes",
        )
        result.update(changes)
        return result

    def search(self, **changes):
        request = dict(scope=self.scope(), query="SQLite", exact_key=None, types=[], limit=50)
        request.update(changes)
        return search_memory(self.db, request, authorized_projects={"p_test"})["data"]

    def resolution(self, conflict, **changes):
        row = self.db.execute(
            "SELECT head_seq FROM conflict_head WHERE id=?", (conflict,)
        ).fetchone()
        request = dict(
            project_id="p_test",
            conflict_id=conflict,
            expected_conflict_seq=row[0],
            action="keep_existing",
            effective_at=None,
            idempotency_key="review",
            rationale="Synthetic review",
        )
        request.update(changes)
        return request

    def test_search_context_scope_time_and_exact_channel(self):
        self.write(record())
        self.assertEqual(len(self.search()["items"]), 1)
        self.assertEqual(self.search(scope=self.scope(known_seq=0))["items"], [])
        self.assertEqual(
            self.search(scope=self.scope(valid_at="2026-11-22T00:00:00Z"))["items"], []
        )
        self.assertEqual(
            len(self.search(query="absent", exact_key=record()["identity"])["items"]), 1
        )
        self.assertEqual(self.search(query='" OR * NOT (')["items"], [])
        with self.assertRaises(ReadError):
            self.search(scope=self.scope(additional_projects=["global"]))
        context = get_project_context(
            self.db, {"scope": self.scope(), "goal": None}, authorized_projects={"p_test"}
        )["data"]
        self.assertEqual(context["items"][0]["score"], 0)
        self.assertEqual(context["budget"]["used"], len(canonical(context)))

    def test_conflict_warning_withholding_and_review_replay(self):
        first = self.write(record())
        candidate = self.write(self.changed(first))["data"]
        result = self.search()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["conflicts"][0]["conflict_id"], candidate["conflict_id"])
        old = self.search(scope=self.scope(known_seq=first["data"]["ledger_seq"]))
        self.assertEqual(len(old["items"]), 1)
        request = self.resolution(candidate["conflict_id"])
        with self.assertRaises(ReadError):
            resolve_conflict(self.db, request, principal_id="agent", review_projects=set())
        ack = resolve_conflict(
            self.db, request, principal_id="reviewer", review_projects={"p_test"}
        )
        self.assertEqual(
            resolve_conflict(self.db, request, principal_id="reviewer", review_projects={"p_test"}),
            ack,
        )
        self.assertEqual(len(self.search()["items"]), 1)
        request["rationale"] = "Changed"
        with self.assertRaises(ReadError):
            resolve_conflict(self.db, request, principal_id="reviewer", review_projects={"p_test"})

    def test_accept_candidate_temporal_projection_and_coexist_rejection(self):
        first = self.write(record())
        candidate = self.write(self.changed(first))["data"]
        request = self.resolution(candidate["conflict_id"], action="coexist")
        with self.assertRaises(ReadError):
            resolve_conflict(self.db, request, principal_id="reviewer", review_projects={"p_test"})
        request.update(action="accept_candidate", effective_at="2026-11-24T10:00:00Z")
        resolve_conflict(self.db, request, principal_id="reviewer", review_projects={"p_test"})
        self.assertEqual(
            self.search(query="PostgreSQL")["items"][0]["revision_id"], candidate["revision_id"]
        )
        self.assertEqual(
            len(self.search(scope=self.scope(valid_at="2026-11-23T12:00:00Z"))["items"]), 1
        )

    def test_history_pinned_continuation_and_small_budget(self):
        first = self.write(record())["data"]
        request = dict(
            project_id="p_test",
            memory_id=first["memory_id"],
            after_seq=0,
            through_seq=None,
            limit=1,
            token_budget=16384,
            tokenizer="utf8_bytes",
        )
        page = get_history(self.db, request, authorized_projects={"p_test"})["data"]
        self.assertIsNotNone(page["next_after_seq"])
        r = record()
        r["idempotency_key"] = "support"
        r["source"]["anchor"] = "second source"
        self.write(r)
        request.update(after_seq=page["next_after_seq"], through_seq=page["through_seq"])
        second = get_history(self.db, request, authorized_projects={"p_test"})["data"]
        self.assertTrue(all(event["seq"] <= page["through_seq"] for event in second["events"]))
        self.assertEqual(second["budget"]["used"], len(canonical(second)))
        request.update(after_seq=0, token_budget=512)
        with self.assertRaises(ReadError):
            get_history(self.db, request, authorized_projects={"p_test"})

    def test_whole_item_budget_unknown_time_and_unicode(self):
        r = record()
        r["statement"] = "SQLite " + "漢字\\n" * 180
        self.write(r)
        result = self.search(scope=self.scope(token_budget=512))
        self.assertEqual(result["items"], [])
        self.assertTrue(result["budget"]["truncated"])
        self.assertLessEqual(len(canonical(result)), 512)
        r = record()
        r["identity"]["key"] = "unknown"
        r["idempotency_key"] = "unknown"
        r["valid_from"] = None
        self.write(r)
        self.assertIn("unknown_validity_excluded", self.search()["diagnostics"])
        self.assertEqual(
            json.loads(canonical(self.search()))["items"][0]["statement"],
            "SQLite " + "漢字\\n" * 180,
        )
