# Brain

Local-first, project-scoped memory for AI tools. Phase 2 is being built one task at a time; see [the task checklist](docs/plans/2026-11-18-phase-2.md) and [architecture](ARCHITECTURE.md).

Tasks 1–2 provide package/configuration, SQLite initialization and persistent Git project resolution. MCP, memory writes/search and transcript discovery are not implemented yet. Runtime currently uses only Python's standard library and performs no model downloads or network calls.

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


## Resolve a project

```sh
uv run brain project resolve --cwd /absolute/path/to/repo --data-dir ./brain-data
```

The explicit admin command initializes the chosen private database if needed and returns JSON containing `project_id`, `resolution` and `needs_review`. Worktrees share the canonical Git common directory. Independent clones can share a project through a normalized origin; repositories without origin remain separate, and directories outside Git resolve to `global`. This provisions namespaces only; no memory cards are recorded or retrieved yet.

Known GitHub/GitLab/Bitbucket HTTPS and conventional Git SSH origins share an identity; host case/default ports and a terminal `.git` are normalized while repository path case and nondefault ports remain distinct. Credentials are removed before persistence/output. Custom HTTPS/absolute SSH URLs remain transport-specific. Uncertain formats (including file/local origins and unknown-host scp-style origins), malformed/bare repositories and multiple origins require explicit intervention rather than guessed identity. Only common-repository local config is read; global/system config, config includes and inherited `GIT_*` overrides are not used. No fetches, checkouts, hooks or network requests occur.

A changed established origin or conflicting aliases returns exit code 2 and `{"project_id":null,"resolution":"ambiguous","needs_review":true}` without changing project mappings. Removing origin preserves the common-directory mapping. A first unclaimed origin may be attached to a previously local-only project. Explicit remapping/merging is not implemented; no existing project history is silently moved. Invalid metadata produces a bounded error (exit 1), not a fallback to global.

