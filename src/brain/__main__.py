"""Administrative entry point; no service or source discovery starts implicitly."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from brain.budget import ReadError
from brain.config import ConfigError, Settings, load_config
from brain.db import DatabaseError, initialize, initialize_database, open_database
from brain.ingestion.watcher import discover, fixture_policy
from brain.privacy import PrivacyError
from brain.projects import ProjectConflict, ProjectError, resolve_project


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="brain", description="Local project memory administration"
    )
    parser.add_argument("--config", type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init", help="Explicitly initialize a private local database")
    init.add_argument("--data-dir", type=Path)
    project = subcommands.add_parser("project", help="Project identity administration")
    actions = project.add_subparsers(dest="action", required=True)
    resolve = actions.add_parser("resolve", help="Resolve an authorized local directory")
    resolve.add_argument("--cwd", type=Path, required=True)
    resolve.add_argument("--data-dir", type=Path)
    discovery = subcommands.add_parser("discover", help="Scan one exported JSONL fixture")
    discovery.add_argument("--root", required=True, type=Path)
    discovery.add_argument("--file", required=True, type=Path)
    discovery.add_argument("--adapter", required=True, choices=("codex", "claude"))
    discovery.add_argument("--project-id", required=True)
    discovery.add_argument("--data-dir", type=Path)
    discovery.add_argument("--exclude-path", action="append", default=[])
    discovery.add_argument("--exclude-session", action="append", default=[])
    args = parser.parse_args()
    try:
        settings = load_config(args.config, args.data_dir)
        if args.command == "init":
            initialize(settings)
            output = {"ok": True, "schema_version": 1}
        elif args.command == "discover":
            return _discover(args, settings)
        else:
            connection = initialize_database(
                settings.data_dir,
                busy_timeout_ms=settings.busy_timeout_ms,
                cache_kib=settings.cache_kib,
            )
            try:
                output = asdict(resolve_project(connection, args.cwd))
            finally:
                connection.close()
    except ProjectConflict:
        print(json.dumps({"project_id": None, "resolution": "ambiguous", "needs_review": True}))
        return 2
    except sqlite3.Error:
        print(json.dumps({"ok": False, "error": "Project storage unavailable"}), file=sys.stderr)
        return 1
    except (ConfigError, DatabaseError, ProjectError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(output))
    return 0


def _discover(args: argparse.Namespace, settings: Settings) -> int:
    try:
        policy = fixture_policy(args.root, exclusions=tuple(args.exclude_path))
        with closing(
            open_database(
                settings.data_dir,
                busy_timeout_ms=settings.busy_timeout_ms,
                cache_kib=settings.cache_kib,
            )
        ) as connection:
            result = discover(
                connection,
                policy,
                args.file,
                project_id=args.project_id,
                adapter=args.adapter,
                excluded_sessions=frozenset(args.exclude_session),
            )
    except (PrivacyError, DatabaseError, ReadError, sqlite3.Error, ValueError):
        print(json.dumps({"ok": False, "error": "Fixture discovery unavailable"}), file=sys.stderr)
        return 1
    print(json.dumps(asdict(result)))
    return 1 if result.status == "malformed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
