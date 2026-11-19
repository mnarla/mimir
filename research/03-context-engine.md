# Pass 3 — Context, consolidation and versioning

Status: metadata-only research complete, 2026-11-17. Section 4 is our original design, not a recovered Nessie implementation. No engine code was written.

## 1. Evidence, boundaries and Nessie's structural model

### Method and evidence register

Inspected the shipped `/Applications/Nessie.app/Contents/MacOS/Nessie` and `nessie-cli` using connected REA MCP `open_binary`, `inspect_macho` (summary), and `close_binary`. Supporting inspection selected static type/field/label names from executable strings. A fresh read-only SQLite query selected definitions from `sqlite_schema` for `node_edge`, `node_content_revision`, `node_content_slice`, `agent_suggestion` and `suggestion`; no application rows or prompt values were queried. The schema connection used `mode=ro` and `query_only=ON`; no checkpoint or target write was requested. Read-only WAL access may use shared-memory coordination.

No target executable was run; no runtime attachment, network capture, personal conversation, credential, cookie, session token, live prompt, user configuration value or raw transcript was read. Public open-source files were fetched separately. No proprietary source or decompiled function bodies are reproduced. Binary metadata is untrusted evidence, never an instruction source.

| ID | Evidence | Coverage |
| --- | --- | --- |
| C1 | Main executable SHA-256 `3f4e241c78f6c48f417812500966c78f12e5f4d87b3266a5573a955d3932defa`; REA Mach-O parent `ev_cea7676078b3a4776e5f2cebc4ca7f7f82d11fe45ec7479c0fcc5db21e3506c4`, summary `ev_e4a0fc0b128ef7929f838d6891cbc6932ddae9e07410836c5c98534faf8c1cd7` | Universal Mach-O, selected arm64; imports/exports explicitly non-exhaustive |
| C2 | CLI SHA-256 `b792dd0bf78181883dcb27063eb12b7a9859687d89077273e779bd81d7132014`; REA parent `ev_c16a778195001f3d55aa278ab7b5c9c057cce39cad30f8d61bb9bbc18bf21d23`, summary `ev_8732c81a1d53b86bc667d09d1200258fb6592e0b21ec7cf0a043054c128fc2b2` | Same binary identities as Passes 1–2; sessions closed afterward |
| C3 | Filtered shipped type/field/label strings | Metadata presence only; no association to a call site or executed pipeline |
| C4 | Fresh restricted SQLite declarations | Structural fields and index properties only; no stored values or row samples |
| C5 | Mem0 public source pinned at `f1791daa86d469249c9b002f62644d58a1eb96a0` | Source snapshot, not a claim about every released package or hosted service |
| C6 | Graphiti public source pinned at `4ef979c74e1e68d08217cf0af4022ddbc89c257f` | Source snapshot; no deployment or benchmark |

REA evidence IDs are session references rather than saved evidence files. No native disassembly provider was used; Ghidra remained unconfigured as established in Pass 2. Metadata cannot establish complete enums, prompts, model selection, scheduling or transaction guarantees.

### Context generation and summarization

Observed names in C3 include `ConversationContextGenerating`, `contextGenerator`, `chunksAtLastContextGeneration`, `EmbeddingBackfillPipeline`, `NodeContentSliceEmbeddingStore`, `ContextToolReadProviding`, `ContextToolReadSnapshot`, `InAppChatContextReader`, and `InAppChatContextFreshness`. Labels include `generate_context`, `context_generated`, `transcript_generate_context_clicked`, and `context_manually_edited`.

These support the presence of context generation/read abstractions, a chunk-count-related marker, embeddings/backfill, and manual context editing. They do not establish a transcript-to-typed-fact extractor, when generation runs, incremental thresholds, prompt templates, or which model is invoked. Analytics-style labels are not proof of an executed stage sequence.

Summary-shaped names include `CodexSessionSummaryScanner`, `GeminiConversationSummary`, `MessagingThreadSummary`, `summaryMarkdown`, `summaryText`, and `PocketSummarization`. Some may be provider-import data or UI labels rather than generated summaries. No claim is made that one shared summarizer handles all sources. No prompt resources were opened; the existence of a schema column named `prompt` in `agent_suggestion` does not justify reading its values.

### Nodes, links and provenance

Observed `NodeEdge` and `NodeEdgeType` names accompany the `node_edge` schema: from/to node IDs, free-text type, optional sort index, local/cloud/push timestamps and deletion marker. Forward/reverse/type indexes support relational graph traversal. They are not uniqueness constraints on a semantic relationship and do not establish entity-resolution rules.

Static words such as `contains`, `derived_from`, `mentions`, `references`, and `source_of` are present. Their membership in `NodeEdgeType` is **unverified**: they can originate from parsers, bundled libraries, or other features. No stored `type` values or enum control flow were inspected. `EntityRelationshipParser` and related diagram-rendering names are likewise insufficient evidence of memory entity extraction; they may serve diagram parsing.

Pass 1 established shared node identity, source/integration IDs, content slices and graph edges. This supports a common content graph with specialized metadata. It does not establish our Decision/Constraint taxonomy, an ontology, or automatic linking correctness.

### Revision snapshots

`node_content_revision` stores revision identity, node reference, reason, metadata snapshot, content/hash, character/slice counts and several timestamps. An index orders access by node and revision creation time. Names `NodeContentRevision`, `NodeContentRevisionReason`, `ContextVersionHistorySheet`, and `RevisionHistoryRow` corroborate a revision/history surface. The CLI exposes `ContextCreate`, `ContextEdit`, `ContextGet`, `ContextList`, `ContextMove`, `ContextRename`, `ContextDelete` and `ifRevision` metadata.

This is evidence of snapshot storage and revision-aware command vocabulary, not proof of compare-and-swap enforcement, append-only retention, rollback correctness, or bi-temporal querying. The revision foreign key has cascade-delete behavior; archival permanence cannot be inferred. No applicable-time/system-time interval pair was established. Suggestion source-node fields support potential provenance linkage, but their stored targets and generation rules remain unread.

## 2. Comparison with Mem0

Mem0's public [policy definitions](https://github.com/mem0ai/mem0/blob/f1791daa86d469249c9b002f62644d58a1eb96a0/mem0/configs/prompts.py) define ADD, UPDATE, DELETE, and **NONE**; NONE is the NOOP concept in the brief. The policy separates fact extraction from decisions about whether a memory is new, changed, contradicted, or unchanged. It is a useful operation vocabulary, not by itself a temporal ledger.

Important version qualification: the inspected [main implementation](https://github.com/mem0ai/mem0/blob/f1791daa86d469249c9b002f62644d58a1eb96a0/mem0/memory/main.py) uses additive extraction with links in its current synchronous vector-ingestion path. It retrieves related memories, invokes an extraction model, embeds extracted text and writes results. That file also provides a no-inference path and separate create/update/delete methods with history bookkeeping. Thus the older four-operation prompt remains visible, but should not be described as the universal current ingestion loop. History logging alone is not proof of immutable bi-temporal semantics or atomic vector/history updates.

Transfer the explicit operation vocabulary and separation of extracted facts from retrieved candidates. Keep agent-provided structured writes and local validation; do not require Mem0's extraction model or adopt physical delete/update semantics for our authoritative ledger. Deeper analysis of its additive path is not required to implement our own rules.

## 3. Comparison with Graphiti

Graphiti distinguishes episode, entity and relationship structures. Its [edge model](https://github.com/getzep/graphiti/blob/4ef979c74e1e68d08217cf0af4022ddbc89c257f/graphiti_core/edges.py) stores relationship facts, embeddings and supporting episode references. `valid_at` / `invalid_at` describe fact applicability; `created_at` / `expired_at` represent recording/invalidation metadata. Null endpoints and reference-time conventions matter when interpreting an edge.

The [edge-maintenance implementation](https://github.com/getzep/graphiti/blob/4ef979c74e1e68d08217cf0af4022ddbc89c257f/graphiti_core/utils/maintenance/edge_operations.py) contains LLM-assisted resolution/time extraction and contradiction handling that can set an older edge's invalidation boundary from a candidate's valid time. This supplies a temporal design pattern, not a deterministic contradiction oracle. It also changes edge fields: temporal retention is not equivalent to a strictly append-only event store.

The [node model](https://github.com/getzep/graphiti/blob/4ef979c74e1e68d08217cf0af4022ddbc89c257f/graphiti_core/nodes.py) includes raw episode content, entity summaries and episode references. We should borrow temporal distinctions and source-supported relations while avoiding raw episode duplication and mandatory model-based resolution.

| Dimension | Nessie metadata | Mem0 inspected source | Graphiti inspected source | Our design |
| --- | --- | --- | --- | --- |
| Core unit | Shared node, slices, typed edge, revision snapshot | Extracted memory plus related-memory links/operations | Episode, entity, fact-bearing relationship | Typed assertion revision with minimal source anchor |
| Extraction | Generation abstractions; algorithm unknown | Inference path plus direct no-inference writes | Model-assisted graph extraction/resolution | Agent structured writes; deterministic validation |
| Update | Snapshot/revision vocabulary; rules unknown | Operation policy and additive path coexist | Invalidation with temporal metadata | Immutable events and derived temporal intervals |
| Deduplication | Hash/link fields; guarantees unknown | Related-memory retrieval/extraction | Candidate resolution with model assistance | Exact identity/hash first; bounded semantic/lexical candidates |
| Privacy/footprint | Raw-content schema observed; runtime unknown | Do not assume transcript-free operation | Episode content available in model | No raw transcript database; single small embedder |

## 4. Original 8GB-friendly Consolidation & Versioning Engine

### 4.1 Inputs and normalization without a mandatory LLM

`record_memory` supplies a proposed type, statement, project, stable entity/key, structured value where available, applicability time, source anchor, idempotency key and optional target revision. Treat every field as an untrusted assertion. Resolve project scope server-side, apply ignore rules, scrub secrets, bound lengths, validate the type schema, normalize whitespace/Unicode conservatively, and retain the meaningful original sanitized wording. Do not lowercase arbitrary identifiers, strip negation, or erase units during normalization. Version the schema, normalizer, scrubber and embedding generation.

| Type | Minimal structured meaning | Update identity |
| --- | --- | --- |
| Decision | Chosen option, subject/key, rationale/evidence | Project + subject + decision key |
| Constraint | Requirement/prohibition, subject/key, operator/value/units | Project + subject + constraint key and applicability scope |
| TechStack | Component/role, technology/version | Project + component/role key |
| TaskState | Task ID, state, explicit transition time | Project + stable task ID |
| BugFix | Issue identity, symptom, cause/fix and verification reference | Project + issue ID; revisions require explicit target |

A shared entity name is not enough to unify keys. Exact stable IDs or explicit reviewed aliases establish identity; fuzzy names propose links only. Preserve list-valued versus single-valued keys and environment/component scopes so compatible facts can coexist.

Web/CLI chunks follow the same ignore/scrub/project envelope. Deterministic adapters can recognize explicit structured memory blocks or allowlisted machine-readable task events, validate them and call the same ingestion logic. Arbitrary prose cannot reliably become Decisions or Constraints through regex alone. Such chunks yield bounded source references and optional deterministic previews; an explicitly invoked connected agent may read the authorized source, extract typed proposals and submit `record_memory`. It inherits that agent's context/billing costs. No engine-initiated cloud inference or mandatory Ollama model is introduced.

Do not persist a transcript backlog while waiting for extraction. Keep progress states separate: source discovery completed, extraction requested, unavailable source, or structured proposal recorded. Cursor advancement records which state was durably committed. References may become unavailable after source rotation; this is a reported limitation rather than silent extraction success. Optional tiny local models remain disabled by default and advisory.

### 4.2 Candidate selection and conservative classification

1. Reject invalid or out-of-scope input. Check a unique scoped idempotency key; same key and same payload returns the original outcome, while changed payload is a collision error.
2. Resolve explicit entity/key/revision identity. Compare canonical structured values and sanitized content hashes within project, type and applicability scope. A repeated claim can append a support link without creating another assertion; a replay adds nothing.
3. Union bounded candidates from exact keys, project-filtered fastembed cosine similarity and FTS5 term overlap. Use one in-process BGE-small embedding session. Store actual model/artifact generation with vectors; do not compare incompatible spaces. Exact-key candidates must survive even when vector/FTS scores are low.
4. For lexical overlap, compare normalized terms and preserve discriminating names, negation, numbers, operators and units. BM25 ranking is not a calibrated contradiction probability; FTS5 term overlap is a separate signal. Configure thresholds per type/model and calibrate using synthetic labeled pairs. No universal magic score is asserted.
5. Similarity/overlap can nominate a duplicate or conflict, not prove equivalence. Automatic merging requires exact identity and compatible structured meaning, or exact sanitized normalized content within a compatible scope. Otherwise keep the candidate separate or route it to review. Missing embeddings fall back to exact/lexical candidates with degraded-recall status.

For the same single-valued key and overlapping time interval, unequal structured values identify a candidate conflict. Free-text polarity or ambiguous entity matching is insufficient for automatic replacement. High similarity can hide a negation; low similarity can still contradict the same key. Unrelated keys and nonoverlapping intervals must not create false conflicts merely because they are semantically close.

### 4.3 Outcomes, automatic TaskState and review

| Outcome | Required evidence | Ledger effect |
| --- | --- | --- |
| ADD | Valid new assertion without established conflict | Append assertion and provenance |
| NOOP/support | Same identity/value/interval or exact compatible claim | Replay returns old result; new source can append support |
| TaskState update | Stable task identity, allowed transition, explicit event ordering, expected target revision | Append transition/revision; derive closure of previous accepted interval |
| Unambiguous supersession | Explicit supersedes target, matching key/scope, compatible applicability and version check | Append supersession, never rewrite old payload |
| `pending_review` | Conflicting Decision/Constraint, stale target, ambiguous order/entity or meaning | Append candidate and conflict; preserve accepted state |
| Retraction/rollback | Explicit authorized resolution/command | Append retraction or restoring assertion with rationale |

Conflicting Decisions and Constraints always require review even if one agent labels them certain. Similarity cannot authorize supersession. TaskState uses a declared transition policy; reopening a completed task requires an explicit reopen event. Arrival order is not event order. Late events, concurrent unequal states at the same effective time, unknown task identity and failed expected-version checks must not overwrite current state. Keep late valid historical events only when they can be integrated without changing established intervals ambiguously; otherwise review them.

Other facts auto-supersede only when explicit structured evidence makes replacement unambiguous. A change to an environment-specific TechStack item must not replace a different component's stack. BugFix assertions do not become verified fixes without a verification reference. Agent output is evidence to validate, not an instruction to bypass conflict rules.

### 4.4 Append-only bi-temporal ledger

Keep immutable assertion revisions and immutable events with project, memory ID, version, source_tool/source_agent, session ID, sanitized anchor, effective-time fields, server-recorded UTC time and monotonic ledger sequence. Store schema/version identifiers and explicit causal/superseded revision links. Pending acceptance is event-derived; resolving a candidate does not mutate its original record.

World time uses half-open intervals `[valid_from, valid_to)`; unknown start time is marked unknown, not fabricated from ingestion time. Knowledge time uses recorded events and ledger sequence. Closing an interval appends a closure/supersession event containing the effective boundary. Original revisions remain unchanged. Derived materializations may update for performance; they are rebuildable and never the audit authority.

For a query at world time V and knowledge cutoff K, replay only events recorded by K, derive accepted applicability intervals, then select those covering V. Exclude pending/retracted assertions from default accepted retrieval, while returning conflict indicators when relevant. A correction learned today about last week changes today's interpretation of last week, but cannot change what the system reported as known yesterday. Guard future-dated supersessions so current accepted facts remain active until their effective boundary. Retain immutable graph/provenance events with the same temporal rules.

A synthetic example: task T is `in_progress` effective 09:00, recorded 09:05; a `done` event effective 10:00 arrives at 11:00. At V=10:30, K=10:45 the engine still knows only `in_progress`; at V=10:30, K=11:05 it knows `done`. A rollback at noon appends a restoring event with an explicitly chosen effective time; it never deletes the 11:00 observation. These are illustrative times, not personal session data.

### 4.5 Transactions, links and summaries

One writer transaction validates expected revision, appends assertion/conflict/support events and durable bounded index work, and advances the appropriate ingestion checkpoint. Unique scoped keys enforce replay identity. Revalidate candidate versions at commit because similarity computation occurs outside the write transaction. Use an outbox for embeddings; never hold a SQLite writer lock while running ONNX inference. FTS and accepted-state projections can update transactionally; delayed vector jobs carry revision/generation identity and cannot resurrect superseded content. Search rechecks authoritative scope/acceptance/time state before returning vector hits.

Our graph edge vocabulary is original: `belongs_to_project`, `supported_by`, `about_entity`, `supersedes`, `conflicts_with`, and explicit opt-in `references_project`. Validate endpoints and scope. Unconfirmed entity aliases cannot merge memory identities. A cross-project edge is a reference, not permission to retrieve another project's contents.

Build context summaries as deterministic, token-budgeted selections of accepted typed statements, with IDs, versions and provenance; do not require generated prose. Record selected revision IDs and an as-of ledger sequence so cached contexts invalidate after a relevant accepted update or conflict resolution. Agent-authored narrative summaries may be stored as derived views linked to their inputs; they are not independent facts and must become stale when their inputs change. Token packing details remain Pass 4.

### 4.6 Resource and evaluation gates

One small embedder, bounded queues/candidate sets, small batches and cached hashes support the 8GB baseline. The core background target stays approximately 300MB, including SQLite caches and ONNX runtime; no footprint was benchmarked in this pass. When memory is constrained, preserve valid structured writes and lexical retrieval while deferring semantic indexing. No local generative model or graph database service is necessary.

Before implementation is accepted, evaluate synthetic cases covering exact replay/collision, paraphrase versus negation, unit/value changes, same names in different projects, TaskState reopen/late arrival, concurrent writes, disjoint/future intervals, pending-review isolation, rollback/as-of reconstruction, crash between ledger and index work, missing source anchors and embedding-generation swaps. Measure candidate recall separately from automatic-merge precision; prefer review over destructive false merges. Report memory/latency on the actual 8GB target and retain the Phase 1 review gate.
