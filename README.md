# Brain

Local-first, project-scoped memory for AI tools. Phase 2 is being built one task at a time; see [the task checklist](docs/plans/2026-11-18-phase-2.md) and [architecture](ARCHITECTURE.md).

The first task provides package/configuration and SQLite initialization only. MCP, memory writes/search and transcript discovery are not implemented yet. Runtime currently uses only Python's standard library and performs no model downloads or network calls.

## Development

Requires Python 3.12+ and uv:

```sh
uv sync --python 3.12
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run mypy
uv run bandit -r src/brain -ll
```

## Local initialization

```sh
uv run brain init --data-dir /absolute/path/to/private/brain-data
```

This explicitly creates a private data directory and `memory.sqlite` with versioned schema. It does not import chats, change agent configs or start a server. Repeating initialization verifies the migration and preserves data. Defaults use `~/.local/share/brain`; initialization is never triggered merely by importing the package.

Optional configuration (`--config /path/to/brain.toml` before the subcommand):

```toml
[storage]
data_dir = "./brain-data"
busy_timeout_ms = 3000
cache_kib = 8192
```

Relative data paths resolve against the config file's directory. CLI `--data-dir` overrides only the configured path. Unknown keys and out-of-range values fail without printing submitted config values. Keep configuration and local databases out of version control. The init command requires a dedicated directory owned by the current user and refuses symlinks for the data directory, database and SQLite sidecars.

See the research documents for the proposed contracts. The ~300MB target and token savings remain unmeasured.
