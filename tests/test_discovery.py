"""Task 8 discovery uses disposable, synthetic transcript files."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from brain.db import initialize_database
from brain.ingestion.watcher import discover
from brain.privacy import PrivacyError, SourcePolicy

STAMP = "2026-11-26T12:00:00.123456789Z"
SECRET = "sk-" + "x" * 24


def line(kind, **fields):
    return json.dumps({"type": kind, "timestamp": STAMP, **fields}).encode() + b"\n"


def codex_message(text):
    return line(
        "response_item",
        payload={
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def claude_message(text, *, session="fixture-session", event="fixture-event"):
    return line(
        "assistant",
        sessionId=session,
        uuid=event,
        message={
            "role": "assistant",
            "content": [
                {"type": "text", "text": text},
                {"type": "tool_use", "input": {"secret": SECRET}},
            ],
        },
    )


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = folder / "fixtures"
        self.root.mkdir()
        self.path = self.root / "transcript.jsonl"
        self.db = self.enterContext(closing(initialize_database(folder / "data")))
        with self.db:
            self.db.execute("INSERT INTO project VALUES ('p_test','now','Synthetic')")

    def scan(self, adapter="codex", **options):
        return discover(
            self.db,
            SourcePolicy(self.root),
            self.path,
            project_id="p_test",
            adapter=adapter,
            **options,
        )

    def test_partial_line_resume_and_replay_only_store_references(self):
        header = line("session_meta", payload={"id": "fixture-session", "cwd": "/fake/private"})
        entry = codex_message("synthetic conversation " + SECRET)
        self.path.write_bytes(header + entry[:-2])
        first = self.scan()
        self.assertEqual(first.status, "partial")
        self.assertEqual(first.committed_offset, len(header))
        self.assertEqual(first.source_refs, ())
        with self.path.open("ab") as stream:
            stream.write(entry[-2:])
        second = self.scan()
        self.assertEqual(len(second.source_refs), 1)
        self.assertEqual(self.scan().source_refs, ())
        self.assertEqual(
            self.db.execute("SELECT kind FROM work_item").fetchall(), [("needs_agent",)]
        )
        self.assertEqual(self.db.execute("SELECT count(*) FROM memory").fetchone()[0], 0)
        dump = "\n".join(self.db.iterdump())
        for text in ("synthetic conversation", SECRET, "/fake/private", str(self.path)):
            self.assertNotIn(text, dump)

    def test_rotation_and_same_inode_rewrite_reset_cursor(self):
        old = claude_message("first")
        self.path.write_bytes(old)
        first = self.scan("claude")
        self.path.rename(self.root / "old.jsonl")
        self.path.write_bytes(old + claude_message("next", event="next"))
        second = self.scan("claude")
        self.assertNotEqual(second.file_generation, first.file_generation)
        self.assertEqual(len(second.source_refs), 1)
        inode = self.path.stat().st_ino
        self.path.write_bytes(claude_message("edit!", event="newer"))
        third = self.scan("claude")
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertNotEqual(third.file_generation, second.file_generation)
        self.assertEqual(len(third.source_refs), 1)

    def test_complete_malformed_line_blocks_cursor(self):
        entry = claude_message("first")
        self.path.write_bytes(
            entry + b'{"type":"user","type":"assistant"}\n' + claude_message("later", event="later")
        )
        first = self.scan("claude")
        self.assertEqual(first.status, "malformed")
        self.assertEqual(first.committed_offset, len(entry))
        self.assertEqual(len(first.source_refs), 1)
        self.assertEqual(self.scan("claude").source_refs, ())

    def test_ignore_exclusion_symlink_and_session_policy(self):
        self.path.write_bytes(claude_message("never record"))
        (self.root / ".memoryignore").write_text("transcript.jsonl\n", encoding="utf-8")
        with self.assertRaises(PrivacyError):
            self.scan("claude")
        (self.root / ".memoryignore").unlink()
        linked = self.root / "linked.jsonl"
        linked.symlink_to(self.path)
        with self.assertRaises(PrivacyError):
            discover(
                self.db, SourcePolicy(self.root), linked, project_id="p_test", adapter="claude"
            )
        self.assertEqual(self.scan("claude", excluded_sessions={"fixture-session"}).source_refs, ())
        self.assertEqual(self.db.execute("SELECT count(*) FROM source_ref").fetchone()[0], 0)

    def test_queue_bound_and_cursor_commit_together(self):
        self.path.write_bytes(claude_message("one") + claude_message("two", event="second"))
        first = self.scan("claude", max_pending_work=1)
        self.assertEqual(first.status, "queue_full")
        self.assertEqual(len(first.source_refs), 1)
        self.assertEqual(first.committed_offset, len(claude_message("one")))
        with self.db:
            self.db.execute("UPDATE work_item SET state='done'")
        self.assertEqual(len(self.scan("claude", max_pending_work=1).source_refs), 1)

    def test_rejects_non_regular_files(self):
        os.mkfifo(self.path)
        with self.assertRaises(PrivacyError):
            self.scan()

    def test_cli_reads_only_an_explicit_fixture_and_rejects_live_root_aliases(self):
        self.path.write_bytes(claude_message("synthetic CLI"))
        cli = [
            sys.executable,
            "-m",
            "brain",
            "discover",
            "--adapter",
            "claude",
            "--project-id",
            "p_test",
            "--data-dir",
            str(self.root.parent / "data"),
            "--file",
            self.path.name,
            "--root",
        ]
        result = subprocess.run([*cli, str(self.root)], check=True, capture_output=True)
        self.assertEqual(len(json.loads(result.stdout)["source_refs"]), 1)
        live = self.root.parent / ".codex"
        live.mkdir()
        (live / self.path.name).write_bytes(claude_message("must not open"))
        alias = self.root.parent / "alias"
        alias.symlink_to(live)
        for denied in (live, alias):
            result = subprocess.run([*cli, str(denied)], check=False, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(str(denied).encode(), result.stderr)
        self.assertEqual(self.db.execute("SELECT count(*) FROM source_ref").fetchone()[0], 1)

    def test_hardlinked_fixture_fails_closed(self):
        self.path.write_bytes(claude_message("synthetic"))
        os.link(self.path, self.root / "other.jsonl")
        with self.assertRaises(PrivacyError):
            self.scan("claude")

    def test_batch_and_byte_bounds(self):
        skipped = line("summary")
        self.path.write_bytes(skipped * 128 + claude_message("last"))
        self.assertEqual(self.scan("claude").status, "batch_limit")
        self.assertEqual(len(self.scan("claude").source_refs), 1)
        self.path.write_bytes(b"a" * 1048577)
        with self.assertRaises(PrivacyError):
            self.scan("claude")

    def test_new_revision_and_exclusion_policy_replay(self):
        self.path.write_bytes(claude_message("first"))
        self.scan("claude", excluded_sessions={"fixture-session"})
        first = self.scan("claude")
        self.assertEqual(len(first.source_refs), 1)
        self.path.write_bytes(claude_message("other"))
        self.assertEqual(len(self.scan("claude").source_refs), 1)
        self.assertEqual(self.scan("claude").source_refs, ())
        self.assertEqual(
            self.db.execute("SELECT extraction_state FROM file_discovery").fetchone()[0],
            "needs_agent",
        )

    def test_database_failure_leaves_no_checkpoint(self):
        self.path.write_bytes(claude_message("one"))
        self.db.executescript("""
            CREATE TEMP TRIGGER reject_checkpoint BEFORE INSERT ON file_discovery
            BEGIN SELECT RAISE(ABORT,'test rollback'); END;
        """)
        with self.assertRaises((ValueError, sqlite3.Error)):
            self.scan("claude")
        for table in ("source_ref", "work_item", "file_discovery"):
            count = dict(
                self.db.execute(
                    "SELECT 'source_ref',count(*) FROM source_ref UNION ALL "
                    "SELECT 'work_item',count(*) FROM work_item UNION ALL "
                    "SELECT 'file_discovery',count(*) FROM file_discovery"
                )
            )
            self.assertEqual(count[table], 0)
