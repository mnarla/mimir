# Pass 2 — Provider sync lifecycles and incremental ingestion

Status: metadata-only pass complete, with control-flow limits below. Inspected 2026-11-17. This pass does not establish that any particular provider sync currently works.

## Privacy boundary and method

Inspected only shipped executables and SQLite declarations. Connected REA MCP tools: `open_binary`, `inspect_macho` (summary), `binary_session`, and `close_binary`. Supporting inspection filtered static binary strings down to type names, selector names, field names and a few CLI help labels. These are metadata observations, not recovered implementation code. Neither executable was launched. No network traffic, runtime attachment, browser profiles, WebKit data files, Keychain, cookies, credentials, session-token values, live conversation rows, or raw logs were inspected. No application-row counts were requested.

The only live SQLite query selected table/index definitions from `sqlite_schema`, using `mode=ro` and `query_only=ON`, restricted to `integration`, `local_discovery_cache`, `local_file_metadata`, `node`, and `node_content_slice`. No source database backup was made and no data rows were exported. As in Pass 1, read-only WAL access may use shared-memory coordination; no checkpoint or target write was requested. No schema values from `connectionConfig` or the discovery cache's `payload` were read. No proprietary algorithms or code are reproduced.

## Evidence register

| ID | Source | Observation / limit |
| --- | --- | --- |
| S1 | REA open, main executable | Universal Mach-O, selected arm64; SHA-256 `3f4e241c78f6c48f417812500966c78f12e5f4d87b3266a5573a955d3932defa` |
| S2 | REA main Mach-O parent `ev_cea7676078b3a4776e5f2cebc4ca7f7f82d11fe45ec7479c0fcc5db21e3506c4` | Summary `ev_e4a0fc0b128ef7929f838d6891cbc6932ddae9e07410836c5c98534faf8c1cd7`; 57 dependencies, 5,299 returned imports, one returned export; symbol inventory explicitly non-exhaustive |
| S3 | REA open, CLI executable | Universal Mach-O, selected arm64; SHA-256 `b792dd0bf78181883dcb27063eb12b7a9859687d89077273e779bd81d7132014` |
| S4 | REA CLI Mach-O parent `ev_c16a778195001f3d55aa278ab7b5c9c057cce39cad30f8d61bb9bbc18bf21d23` | Summary `ev_8732c81a1d53b86bc667d09d1200258fb6592e0b21ec7cf0a043054c128fc2b2`; 15 dependencies, 928 returned imports; inventory non-exhaustive |
| S5 | Direct static metadata filtering of S1/S3 | Provider/parser/session type names, WebKit selectors, CLI help labels; no function bodies or call graph |
| S6 | Fresh restricted SQLite schema query | Columns, keys and index properties discussed below; no rows |
| S7 | REA main `binary_session` | No bound deep-analysis provider; Ghidra reported unavailable/not configured |

Hashes match Pass 1, so these observations refer to the same executable versions. REA IDs identify retained session evidence, not committed evidence files. Both binary sessions were closed. No full disassembly was attempted or analysis engine installed; Mach-O inspection works without Ghidra. Imports, type names and selectors cannot prove callers, branch conditions, ordering, or runtime success. Missing strings do not prove absent functionality.

## 1. Mapping schema patterns to provider lifecycles

Observed provider metadata in the main executable (S5):

| Source | Shipped metadata names | Supported inference / unresolved detail |
| --- | --- | --- |
| Claude.ai | `ClaudeConversationFetcher`, `ClaudeParser`, `ClaudeWebView` | Provider-specific fetching/parsing and a web UI component; API pagination/authentication path unknown |
| ChatGPT | `ChatGPTConversationFetcher`, `ChatGPTParser`, `ChatGPTWebView`, `ChatGPTWebFetching` | Separate provider adapter surface; workspace/account selection and cursor semantics not traced |
| Gemini | `GeminiConversationFetcher`, `GeminiConversationListParser`, `GeminiConversationParser`, `GeminiAccountParser`, `GeminiWebView` | List/detail/account parsing separation is plausible; request protocol and pagination unknown |
| Claude Code | `ClaudeCodeConversationFetcher`, `ClaudeCodeDiscoveryPayload`, `ClaudeCodeModelUsageParser` | Local discovery/parser path distinct from web-provider metadata; actual watched paths and offsets unverified |
| Codex | `CodexConversationFetcher`, `CodexFileDiscovery`, `CodexJSONLParser`, `CodexSessionIndexParser`, `CodexSessionSummaryScanner`, `CodexRolloutSessionMetadata` | File/index/rollout discovery and JSONL parsing vocabulary; incremental byte-reader behavior unverified |

The shared schema has integration identity/platform/account ownership, `status`, `lastSyncedAt`, `webSessionId`, `connectionConfig`, `coolingDownUntil`, and `needsReauth`. Local discovery is keyed by `(integrationId, path)` and stores size, inode, nanosecond mtime, parser/discovery versions, verdict, optional source identity, scan time and an opaque payload. Names `LocalDiscoveryCacheStore` and `LocalDiscoveryVerdict` strengthen the link between this table and local discovery. They do not establish the exact cache-hit predicate or verdict values.

A reasonable architectural interpretation is a shared integration lifecycle with provider-specific discovery/fetch/parse steps. The integration table is not evidence that web adapters use the local-file discovery cache. Nor does `lastSyncedAt` prove an incremental cursor: it may be a health or completion timestamp. Only the default status literal `active` was observed; a complete status enum was not recovered.

The CLI contains source/provider filter help and hook-output vocabulary for Claude Code/Codex. In the targeted metadata search, the app exposed the sync/parser type names while the CLI exposed command/filter labels. This suggests the CLI is a control/access surface, but does not prove where sync executes or that all CLI commands use an app daemon. The CLI was never run, including with `--help`, to avoid initialization side effects.

## 2. Authentication, session boundaries and retry state

Observed static names: `IntegrationWebViewRegistry`, `CachedWebView`, `ProviderReauthenticationRequiredError`, `BrowserCookieImportService`, `BrowserCookieSession`, `BrowserCookieImportError`, and `BrowserCookieReadFailure`. Observed WebKit vocabulary includes `WKWebsiteDataStore`, `dataStoreForIdentifier:`, `defaultDataStore`, `nonPersistentDataStore`, `setWebsiteDataStore:`, `websiteDataStore`, `httpCookieStore`, and `getAllCookies:`.

These establish packaged support for WebKit stores, cookie access/import concepts and reauthentication handling. They **do not** establish that each integration uses a unique persistent store, that cookies are encrypted/exported, or that a particular provider uses cookie import. Persistent/default/ephemeral store selectors can coexist for unrelated features. No store identifier, cookie value or browser credential was retrieved.

The `webSessionId` column can represent an association between an integration and a session, but its storage type alone does not establish whether it is an opaque local identifier, credential-bearing value, or server reference. Treat its values as sensitive and leave them unread. `connectionConfig` is similarly opaque.

`coolingDownUntil` supports representing a retry gate, and `needsReauth` supports persisting an authentication intervention flag. `ProviderReauthenticationRequiredError` is corroborating static vocabulary. None of this proves that HTTP 429 sets cooldown, 401 sets reauthentication, retries use exponential backoff, or sync pauses before every request. Actual timers, response classification and credentials' ownership remain unknown.

Keep source import separate from Nessie cloud replication: `CloudSyncEngine` and `NodeCloudSyncCursor` exist as metadata names, and Pass 1 observed push/cloud timestamp fields. A cloud replication cursor cannot be attributed to ChatGPT, Claude.ai or Gemini ingestion merely because both use sync terminology.

## 3. Incremental state, hashing and duplicate-import limits

| Mechanism | Observed evidence | What it establishes / does not establish |
| --- | --- | --- |
| File discovery cache | Composite integration/path primary key, size/mtime/inode and parser/discovery versions | One cache record per integration/path; possible cheap change detection, not correct rotation or truncation handling |
| External identity lookup | `node` integration/source/kind fields and corresponding index | Efficient lookup possible. This index is **not unique**, so it does not enforce one import per source |
| Source revision hints | `originalUpdatedAt`, `parserVersion`, indexed integration/kind/update fields | Efficient changed-source/reparse selection possible; watermark advancement and timestamp tie handling unknown |
| Local source hash | `local_file_metadata.sourceContentHash`, byte count, extractor version | Hash/version storage available; hashing algorithm, input normalization and skip-on-match branches unknown |
| Slice hash and ordering | Content hash; unique `(nodeId, index)` slice index | Unique slice position within a node, not globally unique content or deduplicated conversations |
| Cursor vocabulary | `nextCursor`, `nextPageToken`, `resumeCursor`, `highWatermark`, `endByteOffset` | Static names exist but are not attributable to specific provider fetchers without references; no provider cursor schema or persistence guarantee established |

The observed ingredients can support duplicate-resistant ingestion, but this pass cannot assert Nessie prevents duplicate imports end to end. Neither key names nor content hashes prove atomic cursor commits, exactly-once effects, crash recovery, source edit handling or deduplication across accounts. Likewise, `lastUploadedContentHash` may concern sharing/upload paths rather than imports.

## 4. Original pipeline proposed for our engine

The following is our design, not a recovered Nessie algorithm.

### Integration lifecycle and authentication boundary

Use adapter states `disabled`, `ready`, `syncing`, `cooldown`, `pending_reauth`, and `error`. Treat cooldown and reauthentication as independent gates: authentication intervention must not disappear when a timer expires. Persist only opaque local session handles and nonsecret adapter configuration. Login/reauthentication occurs interactively inside the optional companion or isolated bridge. The engine receives sanitized source events, not reusable credentials. Account-specific handles must not accidentally merge project namespaces.

On classified throttling, respect provider retry guidance and bounded jittered retries. On classified authentication failure, stop retries and surface `pending_reauth`. Transport errors remain distinct from authentication failures. Browser-based or account-specific provider responses require adapter-specific classification and fixture tests; do not assume standard HTTP status mappings cover every provider.

### Local CLI discovery

Resolve project scope and apply ignore rules before opening a source file. Maintain per-source file-generation identity, committed byte offset, parser version and content fingerprint. Path/size/mtime/inode are useful hints, not sufficient proof of unchanged content. Rotation, replacement, truncation and parser upgrades trigger controlled replay; malformed or incomplete trailing JSONL records do not advance the committed offset. Symlinks must not escape authorized source roots.

Watchers reconcile periodically because filesystem events can be dropped. Read bounded batches and retain only transient sanitized input, structured candidate metadata and source anchors. Do not copy Nessie's opaque cache payload model into a raw transcript backlog. Structured `record_memory` from connected agents remains primary; source discovery does not require a local generative model.

### Incremental web sync and idempotency

Keep pagination continuation separate from the committed incremental watermark. Provider IDs, account/integration identity and source revision form an adapter identity; include project scope and parser/extractor generation in downstream processing keys. Use opaque provider cursors only when the adapter actually supports them. Otherwise use a bounded overlapping time window plus stable ID tie-breakers; do not claim that a success timestamp is a lossless cursor.

Normalize and scrub before deriving persisted content hashes. A hash of the sanitized representation detects exact repeat processing; it cannot guarantee distinction between inputs that differ only in redacted data. Version normalization and scrubbing policy so changes trigger re-evaluation. Semantic near-duplicate detection remains separate from import replay deduplication.

Commit sanitized source-reference effects, structured memory events or durable bounded work references, and checkpoint advancement in one transaction. Downstream jobs must be idempotent too. A crash before commit permits replay; a crash after commit must not create duplicate ledger events. If the original source is no longer available, report the failed work reference instead of silently skipping it. Cursor-only advancement must not discard an outstanding required step.

An authoritative source deletion may append a provenance tombstone; a source absent from an incomplete page is not deletion evidence. Do not automatically erase independently supported memories. Keep raw transcripts out of SQLite, FTS, queue payloads and diagnostics.

### 8GB operating envelope

Use one bounded adapter worker, small pages, persisted backoff gates and the shared in-process embedder. Schedule optional browser bridges separately/on demand; count their memory separately and report it explicitly. The sub-300MB core target is not a guarantee for an entire WebKit/browser or Ollama process tree. No bridge, embedder or model was installed/launched during this pass.

## Remaining visibility limits

Deep call graphs, request ordering, provider endpoint contracts, authentication-store selection, cursor persistence, checkpoint transactions and deduplication algorithms are unverified. Ghidra is unconfigured; no installation is needed to retain these findings. The privacy boundary also deliberately excludes proving behavior with personal account traffic. Future validation should use synthetic fixtures and a separately authorized test account/session, not inspect existing personal conversations.

Pass 3 should address consolidation/extraction independently of sync; Pass 4 should define MCP schemas and retrieval packing. No live account sync was tested and no engine implementation was written.
