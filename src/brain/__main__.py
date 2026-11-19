"""Administrative entry point; no service or source discovery starts implicitly."""

import argparse
import json
import sys
from pathlib import Path

from brain.config import ConfigError, load_config
from brain.db import DatabaseError, initialize


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="brain", description="Local project memory administration"
    )
    parser.add_argument("--config", type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init", help="Explicitly initialize a private local database")
    init.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    try:
        settings = load_config(args.config, args.data_dir)
        initialize(settings)
    except (ConfigError, DatabaseError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "schema_version": 1}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
