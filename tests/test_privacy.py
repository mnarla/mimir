"""Synthetic secrets are constructed in memory; file tests use disposable roots."""

import importlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PrivacyTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("brain.privacy"), "missing privacy boundary")
        return importlib.import_module("brain.privacy")

    def test_scrubs_common_keys_headers_urls_jwt_and_private_key_blocks(self):
        api = self.api()
        secrets = [
            "sk-" + "x" * 40,
            "ghp_" + "a" * 40,
            "AIza" + "a" * 35,
            "AKIA" + "A" * 16,
            "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
        ]
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, api.scrub_text("Evidence " + secret))
        for text in [
            'password="synthetic-secret"',
            "api_key='synthetic-secret'",
            "AWS_SECRET_ACCESS_KEY=synthetic-secret",
            "Bearer synthetic-secret",
            "Cookie: session=synthetic-secret; other=also-private",
            "https://user:synthetic-secret@example.invalid/path?token=also-private",
            "-----BEGIN PRIVATE KEY-----\nsynthetic-secret\n-----END PRIVATE KEY-----",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-secret",
        ]:
            clean = api.scrub_text(text)
            self.assertNotIn("synthetic-secret", clean)
            self.assertNotIn("also-private", clean)
            self.assertEqual(api.scrub_text(clean), clean)
        self.assertEqual(
            api.scrub_text("Use SQLite; token budget is 1024; must not use cloud."),
            "Use SQLite; token budget is 1024; must not use cloud.",
        )

    def test_ignore_exclusions_and_default_secret_files_before_source_open(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".memoryignore").write_text("ignored/\n*.jsonl\n!allowed.jsonl\n")
            for name in ["allowed.jsonl", "blocked.jsonl", ".env", "plain.txt"]:
                (root / name).write_text("password=synthetic-secret")
            (root / "ignored").mkdir()
            (root / "ignored/chat.txt").write_text("ignored")
            (root / "private").mkdir()
            (root / "private/chat.txt").write_text("private")
            policy = api.SourcePolicy(root, exclusions=("private",))
            self.assertEqual(policy.read_text(root / "allowed.jsonl"), "password=[REDACTED]")
            for path in [
                root / "blocked.jsonl",
                root / ".env",
                root / "ignored/chat.txt",
                root / "private/chat.txt",
            ]:
                with self.subTest(path=path), patch("brain.privacy.os.open") as opened:
                    with self.assertRaises(api.PrivacyError):
                        policy.read_text(path)
                    opened.assert_not_called()

    def test_outside_symlinks_nonfiles_and_size_limits_fail_closed(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "root"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("do not ingest")
            (root / "link.txt").symlink_to(outside)
            folder = base / "external"
            folder.mkdir()
            (folder / "chat").write_text("outside")
            (root / "linked-dir").symlink_to(folder, target_is_directory=True)
            (root / "large").write_text("x" * 100)
            policy = api.SourcePolicy(root)
            for p in [outside, root / "link.txt", root / "linked-dir/chat", root / "missing", root]:
                with self.subTest(p=p), self.assertRaises(api.PrivacyError):
                    policy.read_text(p)
            with self.assertRaises(api.PrivacyError):
                policy.read_text(root / "large", max_bytes=10)
            (root / "large").write_bytes(b"\xff")
            with self.assertRaises(api.PrivacyError):
                policy.read_text(root / "large")

    def test_negations_cannot_reenable_secret_files_or_ignored_parent(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".memoryignore").write_text("!.env\nignored/\n!ignored/keep.txt\n")
            (root / ".env").write_text("private")
            (root / "ignored").mkdir()
            (root / "ignored/keep.txt").write_text("private")
            policy = api.SourcePolicy(root)
            for path in [root / ".env", root / "ignored/keep.txt"]:
                with self.subTest(path=path), self.assertRaises(api.PrivacyError):
                    policy.read_text(path)

    def test_unsafe_ignore_file_and_invalid_exclusions_fail_closed(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "root"
            root.mkdir()
            outside = base / "rules"
            outside.write_text("*")
            (root / ".memoryignore").symlink_to(outside)
            with self.assertRaises(api.PrivacyError):
                api.SourcePolicy(root)
            (root / ".memoryignore").unlink()
            for exclusion in ["../escape", "/absolute"]:
                with self.assertRaises(api.PrivacyError):
                    api.SourcePolicy(root, exclusions=(exclusion,))
