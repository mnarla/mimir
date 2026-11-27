"""Task 9's public runbook command and fixture assertions."""

import json
import subprocess
import sys
import unittest
from contextlib import closing

from brain.embeddings import load_vec


class BenchmarkTests(unittest.TestCase):
    def run_benchmark(self, *args):
        result = subprocess.run(
            [sys.executable, "-m", "benchmarks", "--repeats", "2", *args],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_worktree_handoff_and_conflict_through_the_runbook(self):
        report = self.run_benchmark("--demo-only")
        self.assertEqual(
            report["handoff"]["decision"],
            "Use SQLite WAL for the local ledger. "
            "The ledger needs crash recovery and concurrent readers on one machine.",
        )
        self.assertEqual(report["handoff"]["conflict"], "pending_review")
        self.assertEqual(
            report["handoff"]["history"], ["assert", "accept", "assert", "conflict_open"]
        )
        self.assertEqual(report["handoff"]["fallback"], "semantic_unavailable")
        self.assertEqual(report["handoff"]["unrelated_project"], "scope_denied")
        overhead = report["overhead"]
        self.assertGreater(overhead["first_mcp"]["tool_catalog_bytes"], 0)
        self.assertEqual(overhead["repeat_mcp"]["tool_catalog_bytes"], 0)
        self.assertGreater(report["ram"]["peak_sampled_total_kib"], 0)

    def test_three_retrieval_modes_use_native_vectors_and_one_snapshot(self):
        import sqlite3

        with closing(sqlite3.connect(":memory:")) as db:
            if not load_vec(db):
                self.skipTest("native sqlite-vec unavailable")
        retrieval = self.run_benchmark()["retrieval"]
        self.assertEqual(set(retrieval["modes"]), {"fts", "vector", "hybrid"})
        self.assertGreater(retrieval["queries"], 10)
        for result in retrieval["modes"].values():
            self.assertEqual(result["scope_violations"], 0)
            self.assertGreater(result["recall_at_5"], 0)
            self.assertGreater(result["p95_ms"], 0)
