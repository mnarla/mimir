"""Synthetic storage only: never reads real agent sessions or user databases."""

import importlib
import importlib.util
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def api(self, module):
        try:
            spec = importlib.util.find_spec(f"brain.{module}")
        except ModuleNotFoundError:
            spec = None
        self.assertIsNotNone(spec, f"missing {module} API")
        return importlib.import_module(f"brain.{module}")

    def open_db(self):
        connection = self.api("db").initialize_database(self.root / "data")
        self.addCleanup(connection.close)
        return connection

    def test_config_resolves_relative_to_file_and_cli_override(self):
        config = self.root / "brain.toml"
        config.write_text('[storage]\ndata_dir="data"\ncache_kib=1024\n')
        load = self.api("config").load_config
        settings = load(config)
        self.assertEqual(settings.data_dir, self.root / "data")
        self.assertEqual(settings.cache_kib, 1024)
        self.assertEqual(load(config, self.root / "override").data_dir, self.root / "override")

    def test_config_rejects_unknown_keys_and_invalid_bounds_without_echo(self):
        config = self.root / "brain.toml"
        api = self.api("config")
        for payload in [
            'secret="PRIVATE"',
            "[storage]\ncache_kib=true",
            "[storage]\nbusy_timeout_ms=-1",
            "[storage]\ncache_kib=999999",
            '[storage]\nunknown="PRIVATE"',
            "[storage]\ndata_dir=123",
        ]:
            with self.subTest(payload=payload):
                config.write_text(payload)
                with self.assertRaises(api.ConfigError) as caught:
                    api.load_config(config)
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_invalid_toml_and_missing_config_are_bounded_errors(self):
        api = self.api("config")
        with self.assertRaises(api.ConfigError):
            api.load_config(self.root / "absent")
        config = self.root / "brain.toml"
        config.write_text('SECRET = "unclosed')
        with self.assertRaises(api.ConfigError) as caught:
            api.load_config(config)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_init_persists_project_and_reopens_idempotently(self):
        c = self.open_db()
        c.execute("INSERT INTO project VALUES ('test','2026-01-01T00:00:00.000000Z','Test')")
        c.commit()
        c.close()
        c = self.open_db()
        self.assertEqual(
            c.execute("SELECT label FROM project WHERE id=?", ("test",)).fetchone()[0], "Test"
        )
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT count(*) FROM schema_migration").fetchone()[0], 1)

    def test_wal_and_connection_guards(self):
        c = self.open_db()
        for name, expected in [
            ("journal_mode", "wal"),
            ("foreign_keys", 1),
            ("recursive_triggers", 1),
            ("synchronous", 2),
            ("cache_size", -8192),
            ("busy_timeout", 3000),
        ]:
            with self.subTest(pragma=name):
                self.assertEqual(c.execute(f"PRAGMA {name}").fetchone()[0], expected)

    def test_private_directory_database_and_sidecars(self):
        c = self.open_db()
        c.execute("INSERT INTO project VALUES ('test','now','Test')")
        c.commit()
        for p in (self.root / "data").iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, p.name)
        self.assertEqual(stat.S_IMODE((self.root / "data").stat().st_mode), 0o700)

    def test_rejects_unrelated_directory_without_chmod(self):
        data = self.root / "data"
        data.mkdir(mode=0o755)
        marker = data / "unrelated.txt"
        marker.write_text("keep")
        before = data.stat().st_mode
        with self.assertRaises(self.api("db").DatabaseError):
            self.api("db").initialize_database(data)
        self.assertEqual(data.stat().st_mode, before)
        self.assertEqual(marker.read_text(), "keep")

    def test_rejects_data_database_and_sidecar_symlinks(self):
        api = self.api("db")
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(api.DatabaseError):
            api.initialize_database(alias)
        for name in ["memory.sqlite", "memory.sqlite-wal", "memory.sqlite-shm"]:
            with self.subTest(name=name):
                data = self.root / name.replace(".", "_")
                data.mkdir()
                target = self.root / "untouched"
                target.write_bytes(b"unchanged")
                (data / name).symlink_to(target)
                with self.assertRaises(api.DatabaseError):
                    api.initialize_database(data)
                self.assertEqual(target.read_bytes(), b"unchanged")

    def test_schema_checksum_and_future_version_fail_closed(self):
        c = self.open_db()
        c.execute("UPDATE schema_migration SET sha256=?", ("bad",))
        c.commit()
        with self.assertRaises(self.api("db").DatabaseError):
            self.open_db()
        c.execute("PRAGMA user_version=99")
        with self.assertRaises(self.api("db").DatabaseError):
            self.open_db()
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_existing_unversioned_database_is_not_adopted(self):
        data = self.root / "data"
        data.mkdir()
        with sqlite3.connect(data / "memory.sqlite") as c:
            c.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaises(self.api("db").DatabaseError):
            self.api("db").initialize_database(data)
        with sqlite3.connect(data / "memory.sqlite") as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM sqlite_schema WHERE name='project'").fetchone()[0],
                0,
            )

    def test_authority_rejects_update_delete_and_replace(self):
        c = self.open_db()
        c.execute("INSERT INTO project VALUES ('test','now','Test')")
        c.commit()
        for sql in [
            "UPDATE project SET label='bad'",
            "DELETE FROM project",
            "INSERT OR REPLACE INTO project VALUES ('test','now','bad')",
        ]:
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    c.execute(sql)
                c.rollback()

    def test_migration_failure_rolls_back_entire_schema(self):
        api = self.api("db")
        from unittest.mock import patch

        # Inject invalid migration SQL without changing the shipped file.
        with patch("brain.db.migration_sql", return_value="CREATE TABLE partial(x);\nBAD SQL;\n"):
            with self.assertRaises(api.DatabaseError):
                api.initialize_database(self.root / "data")
        with sqlite3.connect(self.root / "data" / "memory.sqlite") as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0], 0)
        self.open_db()

    def test_cli_initializes_only_explicit_target(self):
        # No subprocess execution of agent binaries; only our own module.
        result = subprocess.run(
            [sys.executable, "-m", "brain", "init", "--data-dir", str(self.root / "cli-data")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "cli-data" / "memory.sqlite").exists())
        self.assertIn("schema_version", result.stdout)
