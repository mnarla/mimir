# Brain

Local-first, project-scoped memory for AI tools. Phase 2 is being built one task at a time; see [the task checklist](docs/plans/2026-11-18-phase-2.md) and [architecture](ARCHITECTURE.md).

Tasks 1–6 provide package/configuration, SQLite initialization, persistent Git project resolution, strict contract/privacy helpers, an in-process transactional writer, retrieval/history/review and optional local embedding workers with hybrid search. MCP, public memory commands and transcript discovery are not implemented yet. Core operation requires no model; runtime performs no model downloads or network calls.

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


## Contract and privacy helpers (Task 3)

`brain.contracts.schema_for(name, "Input" | "Output")` exports self-contained schemas for the five planned MCP tools and `/ingest`, packaged in `src/brain/contracts/v1.json`. The registry matches the research contract exactly. `prepare_input(name, bytes_or_dict)` is the future writer/transport boundary: bounded strict JSON, shape validation, copying/scrubbing and timestamp normalization, second validation, and cross-field checks. It rejects duplicate properties, nonfinite numbers, integer-valued floats in integer fields, unknown fields (including raw transcript/cookie fields), mismatched ingest provenance/project, inconsistent extraction state, invalid intervals and TaskState/BugFix identity mismatches. UTC timestamps require uppercase T/Z and at most microsecond precision; no naive/offset/leap-second values. Errors contain fixed summaries rather than submitted values. Database version checks and conflict creation are handled by Task 4; transport authentication and conflict resolution remain later tasks.

Strings are scrubbed for common provider keys, AWS access identifiers, JWTs, Bearer headers, secret assignments, cookie headers, URL userinfo and PEM private-key blocks. Nested card content and source anchors are covered. Identity/control fields containing recognized secrets are rejected rather than renamed. Output validation rejects unsafe content instead of silently changing a previously budgeted result. **Redaction is best effort**, not proof of complete secret detection. No payload is persisted or logged by these helpers.

`brain.privacy.SourcePolicy(explicit_root, exclusions=("private",))` takes a snapshot of the root `.memoryignore` (Git-style patterns), then provides `read_text(relative_or_absolute_path)`. It checks exclusions/ignored parents before opening source files, rejects paths outside the root and symlinks in the traversal, reads only bounded regular UTF-8 files, and returns scrubbed text. Built-in `.git/`, `.env`, `.env.*` and `.memoryignore` exclusions cannot be negated; directory exclusions cannot be bypassed by re-including a child. Recreate the policy when root ignore rules change; nested `.memoryignore` files are not loaded in v1. The default source-read cap is 256KiB (explicit maximum 1MiB), and root ignore files are limited to 64KiB. This is a Unix/Apple Silicon helper, not an OS sandbox against another process running as the same user.

There is still no public memory-write command, watcher, HTTP endpoint or MCP server. Task 4 connects prepared structured writes to the transactional ledger through the Python API. JSON Schema and pathspec runtime dependencies are pinned in pyproject/uv.lock; no embedding or generative model dependencies are required for Task 3.


## Structured ledger (Task 4)

`brain.ledger.record_memory` takes an initialized idle SQLite connection, a record contract payload, and trusted `principal_id`/`authorized_projects` keyword arguments. Projects must be provisioned first. The caller receives a validated record_memory success envelope after commit; failures roll back authority, projections and queued jobs together. This is an internal API; transport authentication and public memory CLI commands are pending.

Writes provide sanitized idempotent replay, exact-claim support provenance, expected-head checks, immutable assertions/events, ordered TaskState transitions with explicit reopening, and pending-review conflicts that preserve accepted state. Explicit forward-effective TechStack/BugFix replacements preserve earlier knowledge and valid-time prefixes. Differing claims, including set members, conservatively require review unless a safe automatic update applies. No semantic similarity or prose-equivalence inference is performed. See the Task 4 checkpoint for transition and provenance rules.

FTS documents are updated in the same transaction. Optional embedding work is queued only for enabled generations; the default cap is 1000 pending/leased jobs. No model is downloaded or loaded by the writer. `index_state` reports `degraded` without semantic work, `pending` for queued work and `ready` when enabled generations have vectors. Task 6 provides the optional worker described below.

## Retrieval, history and review (Task 5)

Python callers can use `brain.retrieval.search_memory` / `get_project_context`, `brain.history.get_history`, and `brain.review.resolve_conflict` with the packaged contract payloads. Reads require `authorized_projects`; review requires a trusted `principal_id` and separate `review_projects`. These capabilities must be derived from credentials by the future transport, not from untrusted request bodies.

Retrieval combines exact identity and FTS keyword ranking within one authorized project/world-time/knowledge snapshot. Unknown validity is excluded with a diagnostic. Pending conflicts warn and withhold affected streams until review. No-goal context has deterministic type priorities. Without a working optional semantic channel, search reports `semantic_unavailable`. Queries have bounded candidate counts, eligible scans and SQL deadlines.

Responses pack whole items with provenance within a canonical data JSON budget, including warning metadata and the count itself. The default `utf8_bytes` counter reports a UTF-8 upper bound, not measured host tokens; a trusted pinned `TokenCounter` can supply configured counts. History pins its upper sequence and stops at the first event that cannot fit, preserving continuation order. Review supports keeping the accepted assertion, accepting a candidate at an effective boundary, or explicit compatible coexistence. Review uses version checks, idempotent receipts and atomic temporal projection updates. See the Task 5 checkpoint for bounds and validation evidence.

## Optional semantic channel (Task 6)

`uv sync --extra semantic` installs pinned FastEmbed/sqlite-vec packages, without provisioning any model. Native cosine needs a Python build with SQLite extension loading; the current system Python 3.12 lacks it and safely returns lexical results. Native cosine was verified using the existing Homebrew Python 3.14 and synthetic vectors. Real model RSS, latency and inference quality remain unmeasured.

Python callers explicitly register a `brain.embeddings.Generation` using `brain.worker.register_generation`, queue existing revisions through bounded `backfill`, and call `run_once(connection, shared_embedder)` to process one job. New writes enqueue generation-specific jobs automatically. The worker uses expiring leases, bounded retries and stale-result checks; vectors and job completion commit together. No daemon or public worker command is started.

`LocalEmbedder(generation, directory, manifest)` requires manually provisioned, trusted model artifacts. The manifest maps relative filenames to SHA256 digests; generation.artifact_digest is the SHA256 of canonical manifest JSON and tokenizer_digest matches tokenizer.json. The supported default is BAAI/bge-small-en-v1.5, 384 unit-normalized dimensions, with model_optimized.onnx and tokenizer/config files. All directory files must be verified. One lazy process-wide FastEmbed session uses the explicit local path, local_files_only and one CPU thread. No automatic model download or paid fallback exists. Real model validation requires a separately authorized provisioning/integration run.

Search/context functions accept `embedder=shared_embedder`. Exact identity, keyword and filtered scalar-cosine channels fuse deterministically; unavailable semantics retain lexical/exact results with diagnostics, and partial indexing reports semantic_partial/index_lag. No-goal context skips inference. Semantic configuration is optional and requires both fields; it records the service's intended settings without constructing a model:

```toml
[semantic]
generation_id = "bge-small-v1"
model_dir = "./models/bge-small"
```

Run model-free semantic fixtures with `uv run python -m unittest discover -s tests -p test_semantic.py -v`. Native cases skip when extension loading is unavailable. Generation registration, backfill and model construction are trusted local administration APIs; transport authentication remains Task 7.
