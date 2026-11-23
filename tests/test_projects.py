"""Project identity tests use only disposable Git repositories, never user remotes."""

import importlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from brain.db import initialize_database


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = initialize_database(self.root / "data")
        self.addCleanup(self.db.close)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("brain.projects"), "missing project resolver")
        return importlib.import_module("brain.projects")

    def git(self, cwd, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.run(
            ["git", "-C", str(cwd), *args], env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def repo(self, name, remote=None):
        p = self.root / name
        p.mkdir()
        self.git(p, "init", "-b", "main")
        if remote:
            self.git(p, "remote", "add", "origin", remote)
        return p

    def test_common_dir_and_linked_worktree_share_identity(self):
        api = self.api()
        p = self.repo("main")
        self.git(
            p,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "synthetic",
        )
        w = self.root / "worktree"
        self.git(p, "worktree", "add", "-b", "feature", str(w))
        a = api.resolve_project(self.db, p)
        b = api.resolve_project(self.db, w)
        self.assertEqual(a.project_id, b.project_id)
        self.assertEqual(b.resolution, "common_dir")
        self.assertEqual(self.db.execute("SELECT count(*) FROM project").fetchone()[0], 1)

    def test_independent_clones_share_supported_remote_identity(self):
        api = self.api()
        p = self.repo("one", "https://user:SECRET@GitHub.com/Owner/Repo.git")
        q = self.repo("two", "git@github.com:Owner/Repo.git")
        a = api.resolve_project(self.db, p)
        b = api.resolve_project(self.db, q)
        self.assertEqual(a.project_id, b.project_id)
        self.assertEqual(b.resolution, "remote")
        aliases = str(self.db.execute("SELECT * FROM project_alias").fetchall())
        self.assertNotIn("SECRET", aliases)
        self.assertNotIn("user:", aliases)

    def test_actual_local_clone_and_symlink_reuse_project(self):
        api = self.api()
        source = self.repo("source", "https://github.com/Owner/Example")
        self.git(
            source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "synthetic",
        )
        clone = self.root / "clone"
        self.git(self.root, "clone", str(source), str(clone))
        self.git(clone, "remote", "set-url", "origin", "git@github.com:Owner/Example.git")
        a = api.resolve_project(self.db, source)
        b = api.resolve_project(self.db, clone)
        self.assertEqual(a.project_id, b.project_id)
        alias = self.root / "source-link"
        alias.symlink_to(source, target_is_directory=True)
        self.assertEqual(api.resolve_project(self.db, alias).project_id, a.project_id)

    def test_unrelated_local_repos_and_global_are_isolated(self):
        api = self.api()
        a = api.resolve_project(self.db, self.repo("one"))
        b = api.resolve_project(self.db, self.repo("two"))
        plain = self.root / "plain"
        plain.mkdir()
        g = api.resolve_project(self.db, plain)
        self.assertEqual(g.project_id, "global")
        self.assertEqual(api.resolve_project(self.db, plain), g)
        self.assertEqual(len({a.project_id, b.project_id, g.project_id}), 3)

    def test_remote_normalization_preserves_path_ports_and_unknown_transports(self):
        normalize = self.api().normalize_remote
        self.assertEqual(
            normalize("ssh://git@github.com:22/Owner/Repo.git"),
            normalize("https://github.com:443/Owner/Repo"),
        )
        self.assertNotEqual(
            normalize("https://github.com/Owner/Repo"), normalize("https://github.com/owner/repo")
        )
        self.assertNotEqual(
            normalize("ssh://git@github.com:2222/Owner/Repo"),
            normalize("https://github.com/Owner/Repo"),
        )
        self.assertNotEqual(
            normalize("ssh://git@code.example/Owner/Repo"),
            normalize("https://code.example/Owner/Repo"),
        )
        self.assertNotEqual(
            normalize("https://code.example/repo.git"), normalize("https://code.example/repo")
        )
        for value in [
            "ext::do-not-run",
            "file:///tmp/repo",
            "https://host/repo?secret=x",
            "https://host/repo#fragment",
            "https://host/a/../b",
            "not-a-remote",
        ]:
            with self.subTest(value=value), self.assertRaises(self.api().ProjectError):
                normalize(value)

    def test_changed_origin_needs_review_without_mutation(self):
        api = self.api()
        p = self.repo("one", "https://github.com/Owner/Old")
        original = api.resolve_project(self.db, p)
        before = self.db.total_changes
        self.git(p, "remote", "set-url", "origin", "https://github.com/Owner/New")
        with self.assertRaises(api.ProjectConflict):
            api.resolve_project(self.db, p)
        self.assertEqual(self.db.total_changes, before)
        self.git(p, "remote", "remove", "origin")
        self.assertEqual(api.resolve_project(self.db, p).project_id, original.project_id)

    def test_conflicting_common_dir_and_remote_aliases_are_not_merged(self):
        api = self.api()
        p = self.repo("one")
        q = self.repo("two", "https://github.com/Owner/Two")
        a, b = api.resolve_project(self.db, p), api.resolve_project(self.db, q)
        self.git(p, "remote", "add", "origin", "https://github.com/Owner/Two")
        with self.assertRaises(api.ProjectConflict):
            api.resolve_project(self.db, p)
        self.assertNotEqual(a.project_id, b.project_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM project").fetchone()[0], 2)
        self.assertFalse(self.db.in_transaction)

    def test_first_origin_can_be_attached_to_local_project(self):
        api = self.api()
        p = self.repo("one")
        first = api.resolve_project(self.db, p)
        self.git(p, "remote", "add", "origin", "https://github.com/Owner/One")
        self.assertEqual(api.resolve_project(self.db, p).project_id, first.project_id)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM project_alias WHERE kind='remote'").fetchone()[0],
            1,
        )

    def test_environment_overrides_do_not_redirect_discovery(self):
        api = self.api()
        p, q = self.repo("one"), self.repo("two")
        first = api.resolve_project(self.db, p)
        with patch.dict(os.environ, {"GIT_DIR": str(q / ".git"), "GIT_WORK_TREE": str(q)}):
            self.assertEqual(api.resolve_project(self.db, p).project_id, first.project_id)

    def test_invalid_paths_bare_repo_and_multiple_origins_fail_closed(self):
        api = self.api()
        with self.assertRaises(api.ProjectError):
            api.resolve_project(self.db, self.root / "missing")
        bare = self.root / "bare"
        bare.mkdir()
        self.git(bare, "init", "--bare")
        with self.assertRaises(api.ProjectError):
            api.resolve_project(self.db, bare)
        p = self.repo("one", "https://github.com/Owner/One")
        self.git(p, "config", "--add", "remote.origin.url", "https://github.com/Owner/Two")
        with self.assertRaises(api.ProjectError):
            api.resolve_project(self.db, p)
        self.assertEqual(self.db.execute("SELECT count(*) FROM project").fetchone()[0], 0)

    def test_malformed_urls_and_broken_git_metadata_fail_closed(self):
        api = self.api()
        for remote in ["https://[BROKEN/repo", "ssh://git@host:invalid/Repo"]:
            with self.subTest(remote=remote), self.assertRaises(api.ProjectError):
                api.normalize_remote(remote)
        broken = self.root / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent-synthetic-git-dir")
        nested = broken / "nested"
        nested.mkdir()
        with self.assertRaises(api.ProjectError):
            api.resolve_project(self.db, nested)
        self.assertEqual(self.db.execute("SELECT count(*) FROM project").fetchone()[0], 0)

    def test_caller_transaction_is_not_committed_or_rolled_back(self):
        api = self.api()
        self.db.execute("INSERT INTO project VALUES('pending','now','Pending')")
        with self.assertRaises(api.ProjectError):
            api.resolve_project(self.db, self.repo("one"))
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()

    def test_concurrent_resolution_creates_one_identity(self):
        api = self.api()
        p = self.repo("one", "https://github.com/Owner/One")

        # Separate connections, as clients of the future service would use.
        def resolve(_):
            with closing(sqlite3.connect(self.root / "data/memory.sqlite", timeout=5)) as c:
                return api.resolve_project(c, p).project_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(resolve, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.db.execute("SELECT count(*) FROM project").fetchone()[0], 1)

    def test_cli_returns_resolution_and_safe_conflict(self):
        self.api()
        p = self.repo("one", "https://user:SECRET@github.com/Owner/One")
        args = [
            sys.executable,
            "-m",
            "brain",
            "project",
            "resolve",
            "--cwd",
            str(p),
            "--data-dir",
            str(self.root / "data"),
        ]
        good = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertFalse(json.loads(good.stdout)["needs_review"])
        self.git(p, "remote", "set-url", "origin", "https://user:SECRET@github.com/Owner/Two")
        bad = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertTrue(json.loads(bad.stdout)["needs_review"])
        self.assertNotIn("SECRET", bad.stdout + bad.stderr)
