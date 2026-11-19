# Pass 1 — App stack and local schema

Status: completed within metadata-only scope. Inspected 2026-11-17. Passes 2–4 remain pending.

## Scope and method

Read-only inspection of `/Applications/Nessie.app` and `~/Library/Application Support/Nessie`. No application rows, conversation text, embeddings, credentials, account values, or transcript files were queried. JSON configuration inspection returned top-level key names only. No app launch, sync request, runtime attachment, or decompilation was performed. No proprietary implementation code is reproduced here.

Tools: connected REA MCP `open_bundle_resources`, `inspect_plist`, and `inspect_sqlite_database`; supporting `file`, `otool -L`, filtered symbol-marker checks, executable hashes, filename inventory, and Python SQLite metadata queries. REA's resource session was closed afterward.

The live database was opened with `mode=ro` and `query_only=ON`; the sole SQL query selected definitions from `sqlite_schema`. A temporary empty reconstruction was made from those definitions so REA never received the live database or any user rows. Ordinary tables/indexes/triggers were recreated; FTS5 shadows were regenerated empty; vec0's declaration was retained as schema metadata without loading its extension or reproducing its shadow storage. This is a schema derivative, not a database backup. The initial malformed reconstruction failed REA validation; a corrected reconstruction succeeded. No target writes or database checkpoints were issued. A read-only WAL connection can interact with SQLite shared-memory coordination; this is not a claim of filesystem-level forensic immutability.

## Evidence register

| ID | Source | Evidence and limits |
| --- | --- | --- |
| E1 | REA plist `ev_a005b9befe896f1b909adabcfbf66716573fc4763c2563f28ed1e71e16fc54a8` | Bundle identity/version and resource metadata; no executable identity binding |
| E2 | REA schema `ev_a65a8591d5c791ea1b24b3602f3b0601f8a86f0245a31af7babe2d41a5bb70d7` | Empty derivative SHA-256 `261708f12c2d335d327a8a45a68ec3d3bd64a5adff19d02ceb266a75693d50d1`; `rows=null`; virtual columns not expanded |
| E3 | Direct metadata inspection | Original SQLite declarations, bundle inventory, linked libraries, executable names and hashes; no stored record values |
| E4 | Process/config metadata | Executable paths only, standard launch-directory filenames, top-level JSON keys only; point-in-time coverage |

E2's SQLite 3.54.0 identifies REA's inspection engine, not Nessie's bundled SQLite version. REA IDs are session references; this document preserves the relevant observations without exporting a full evidence bundle. Temporary schema derivatives contain no conversation rows and are not project artifacts.

## App architecture

Observed (E1/E3): Nessie `1.4.6076`, build `6156`, bundle ID `com.nessielabs.Nessie`, minimum macOS 14. Both `Nessie` and `nessie-cli` are universal Mach-O executables (arm64/x86_64). Linked frameworks include SwiftUI, AppKit, WebKit, JavaScriptCore, Network, Security, CryptoKit, and system SQLite. Bundled resource markers include GRDB, GCDWebServer, Swift Crypto, and Swift NIO; Sparkle is a bundled update framework.

Interpretation: a native Swift UI with web-content capabilities, SQLite persistence, and a companion CLI is strongly supported. Resource markers indicate packaged dependencies, not proof that particular features use them. This is not an Electron app. WebKit presence does not establish authentication or browser-sync implementation.

Observed (E4): the main Nessie executable was running; no Nessie-named entries were present in user/system LaunchAgents or system LaunchDaemons. There was no separate Nessie-named process in the executable-name inventory. Background work inside the app is possible; a daemon, startup mechanism, listening port, and scheduler were not established. Filenames alone cannot exclude differently named services.

## Storage and graph

Observed location: `notes.sqlite` with sibling WAL/SHM and a process-lock filename. JSON configuration key names indicate account/device selection and a `CloudSyncEnabled` setting; values were deliberately not read. No claim is made about whether cloud sync is enabled.

| Schema family | Observed structural fields | Architectural interpretation |
| --- | --- | --- |
| `node` | identity, kind, integration/source IDs, workspace path, repository remote/key, local/original timestamps, push/cloud timestamps, deletion marker | Shared entity identity with provenance and sync bookkeeping |
| `node_edge` | from/to IDs, type, sort order, timestamps, deletion marker | Typed graph in relational storage |
| `node_content_slice` | node ID, ordinal, content hash, modality | Ordered chunks separate from identity |
| `node_content_revision` | node ID, reason, snapshot content/hash, timestamps, slice/character counts | Revision snapshot storage; append-only enforcement and rollback semantics unverified |
| `integration` | platform, sync timestamp/status, web session/config fields, cooldown, reauthentication flag | Per-connector lifecycle metadata; no authentication mechanism established |
| `local_discovery_cache` | path, size, nanosecond mtime, inode, parser/discovery versions, verdict | Incremental file-discovery cache is plausible; actual invalidation algorithm unknown |
| `local_file_metadata` | content hash, byte count, extractor version | Content/version-based ingestion metadata |
| Session usage tables | model/provider token counters; skill/session links | Usage/provenance accounting |
| Conversation-specific tables | node-linked role/tool metadata and participant relations | Normalized specializations of the common graph |

Do not adopt conversation-storage tables: our approved design retains extracted memories and source anchors rather than duplicating transcripts.

## Search and indexes

Observed (E2/E3): external-content FTS5 tables for node names and content slices, with insert/update/delete maintenance triggers. A vec0 virtual table declares 3072-dimensional float embeddings and cosine distance. Its shadow-table names are consistent with sqlite-vec storage. The schema does not identify the embedding provider/model, whether embeddings are generated locally, whether vectors are populated, or whether queries combine keyword and vector results. RRF, ranking weights, token limits, and deduplication are unknown.

Observed indexes cover node kind/source/integration, source update timestamps, forward/reverse typed edges, revisions by node/time, unique slice order, dirty push state, tombstones, and pending/acknowledged collaboration updates. These support targeted scans; actual query plans and performance were not measured.

SQLite documents external-content FTS maintenance and BM25 separately from application ranking: [FTS5](https://www.sqlite.org/fts5.html). Vector shadow-storage interpretation is supported by the extension's [architecture](https://github.com/asg017/sqlite-vec/blob/main/ARCHITECTURE.md).

## Versioning and synchronization boundaries

Observed: node and edge local/cloud/push timestamps and deletion markers; context collaboration tables containing sequence/checkpoint, generation, operation, acknowledgment, and state-vector fields. Partial indexes distinguish pending from sequenced updates. These establish durable sync bookkeeping and collaboration-shaped storage. They do not establish a specific remote protocol, working CRDT reconciliation, active usage, or browser chat import logic.

No explicit valid-time/transaction-time pair was established in these relevant declarations. Revisions plus timestamps are insufficient evidence of bi-temporal semantics. Our original ledger must define those semantics explicitly.

## Implications for our engine

Adopt the general patterns of separated identity/content/provenance, relational graph edges, indexed revision access, and durable adapter checkpoints. Use our own schema and terminology. Do not copy Nessie's cloud replication, collaboration machinery, 3072-dimensional vector choice, or transcript retention.

Target an 8GB Apple Silicon machine. Prefer structured agent writes and one small in-process embedder. FTS5 and vectors remain rebuildable projections; the ledger is authoritative. Default to one bounded background worker and short SQLite transactions. WAL supports concurrent reads with a single writer; checkpoint and busy-timeout behavior require evaluation ([SQLite WAL](https://www.sqlite.org/wal.html)).

## Open questions for subsequent passes

- Pass 2: authentication ownership, incremental cursors, replay/idempotency, retry and deletion behavior. Current schema establishes fields only.
- Pass 3: extraction/consolidation rules and prompts. None inspected or inferred here.
- Pass 4: actual MCP contracts, response packing, ranking and project isolation.
- Mem0 and Graphiti comparisons are pending; neither is represented as inspected in Pass 1.
