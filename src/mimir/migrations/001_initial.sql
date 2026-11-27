CREATE TABLE project (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
 label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 160)
) STRICT;
CREATE TABLE project_alias (
 kind TEXT NOT NULL CHECK(kind IN ('common_dir','remote','explicit')),
 identity TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES project(id),
 created_at TEXT NOT NULL, PRIMARY KEY(kind,identity)
) STRICT;
CREATE TABLE source_ref (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(id),
 source_tool TEXT NOT NULL, source_agent TEXT, session_id TEXT NOT NULL,
 source_timestamp TEXT NOT NULL, anchor TEXT NOT NULL CHECK(length(anchor) BETWEEN 1 AND 160),
 locator TEXT CHECK(locator IS NULL OR length(locator)<=512),
 source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
 sanitized_digest TEXT NOT NULL CHECK(length(sanitized_digest)=64),
 parser_generation TEXT NOT NULL,
 UNIQUE(project_id,source_tool,session_id,source_id,source_revision,parser_generation),
 UNIQUE(project_id,id)
) STRICT;
CREATE TABLE memory (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(id),
 entity_key TEXT NOT NULL, fact_key TEXT NOT NULL, applicability_scope TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('Decision','Constraint','TechStack','TaskState','BugFix')),
 cardinality TEXT NOT NULL CHECK(cardinality IN ('single','set')),
 created_at TEXT NOT NULL, UNIQUE(project_id,id)
) STRICT;
CREATE INDEX memory_key ON memory(project_id,entity_key,fact_key,applicability_scope,kind);
-- Different assertions may intentionally share a key; acceptance rules govern coexistence.
CREATE TABLE memory_revision (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, memory_id TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0),
 statement TEXT NOT NULL CHECK(length(statement) BETWEEN 1 AND 2000),
 typed_json TEXT NOT NULL CHECK(json_valid(typed_json)),
 normalized_hash TEXT NOT NULL CHECK(length(normalized_hash)=64),
 normalizer_generation TEXT NOT NULL,
 valid_from TEXT, valid_to TEXT, recorded_at TEXT NOT NULL,
 source_ref_id TEXT NOT NULL,
 CHECK(valid_to IS NULL OR (valid_from IS NOT NULL AND valid_to>valid_from)),
 FOREIGN KEY(project_id,memory_id) REFERENCES memory(project_id,id),
 FOREIGN KEY(project_id,source_ref_id) REFERENCES source_ref(project_id,id),
 UNIQUE(memory_id,version), UNIQUE(project_id,id), UNIQUE(project_id,memory_id,id)
) STRICT;
CREATE TABLE ledger_event (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id TEXT NOT NULL, memory_id TEXT NOT NULL, revision_id TEXT NOT NULL,
 event TEXT NOT NULL CHECK(event IN ('assert','accept','support','close','supersede','conflict_open','conflict_resolve','retract','restore')),
 effective_at TEXT, recorded_at TEXT NOT NULL, principal_id TEXT NOT NULL,
 source_ref_id TEXT, related_revision_id TEXT, conflict_id TEXT,
 details_json TEXT NOT NULL CHECK(json_valid(details_json)),
 summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 1000),
 FOREIGN KEY(project_id,memory_id,revision_id) REFERENCES memory_revision(project_id,memory_id,id),
 FOREIGN KEY(project_id,related_revision_id) REFERENCES memory_revision(project_id,id),
 FOREIGN KEY(project_id,source_ref_id) REFERENCES source_ref(project_id,id)
) STRICT;
CREATE INDEX ledger_memory ON ledger_event(project_id,memory_id,seq);
CREATE INDEX ledger_conflict ON ledger_event(project_id,conflict_id,seq);
CREATE TABLE relation_event (
 id TEXT PRIMARY KEY, seq INTEGER NOT NULL REFERENCES ledger_event(seq),
 project_id TEXT NOT NULL REFERENCES project(id),
 from_revision_id TEXT NOT NULL,
 relation TEXT NOT NULL CHECK(relation IN ('belongs_to_project','supported_by','about_entity','supersedes','conflicts_with','references_project')),
 target_kind TEXT NOT NULL CHECK(target_kind IN ('project','source','entity','revision')),
 target_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('assert','retract')),
 FOREIGN KEY(project_id,from_revision_id) REFERENCES memory_revision(project_id,id)
) STRICT;
-- Polymorphic targets and cross-project opt-in are validated inside the writer transaction.
CREATE TABLE idempotency_receipt (
 project_id TEXT NOT NULL REFERENCES project(id), principal_id TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation IN ('record_memory','resolve_conflict','ingest','rollback')),
 request_key TEXT NOT NULL, request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
 response_json TEXT NOT NULL CHECK(json_valid(response_json)), created_at TEXT NOT NULL,
 PRIMARY KEY(project_id,principal_id,operation,request_key)
) STRICT;

-- Rebuildable projections start here; these can be updated/deleted.
CREATE TABLE accepted_segment (
 id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, memory_id TEXT NOT NULL,
 revision_id TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
 known_from_seq INTEGER NOT NULL REFERENCES ledger_event(seq),
 known_to_seq INTEGER REFERENCES ledger_event(seq),
 CHECK(valid_to IS NULL OR (valid_from IS NOT NULL AND valid_to>valid_from)),
 CHECK(known_to_seq IS NULL OR known_to_seq>known_from_seq),
 FOREIGN KEY(project_id,memory_id,revision_id) REFERENCES memory_revision(project_id,memory_id,id)
) STRICT;
CREATE INDEX accepted_scope ON accepted_segment(project_id,known_from_seq,known_to_seq,valid_from,valid_to);
CREATE INDEX accepted_memory ON accepted_segment(project_id,memory_id,revision_id);
CREATE TABLE conflict_head (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
 memory_id TEXT NOT NULL, accepted_revision_id TEXT NOT NULL, candidate_revision_id TEXT NOT NULL,
 opened_seq INTEGER NOT NULL REFERENCES ledger_event(seq),
 head_seq INTEGER NOT NULL REFERENCES ledger_event(seq),
 status TEXT NOT NULL CHECK(status IN ('pending_review','resolved')),
 FOREIGN KEY(project_id,memory_id,accepted_revision_id) REFERENCES memory_revision(project_id,memory_id,id),
 FOREIGN KEY(project_id,candidate_revision_id) REFERENCES memory_revision(project_id,id)
) STRICT;
CREATE INDEX conflict_pending ON conflict_head(project_id,status,memory_id);
CREATE TABLE search_doc (
 id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, revision_id TEXT NOT NULL,
 statement TEXT NOT NULL, identity_text TEXT NOT NULL,
 FOREIGN KEY(project_id,revision_id) REFERENCES memory_revision(project_id,id),
 UNIQUE(revision_id)
) STRICT;
CREATE VIRTUAL TABLE search_fts USING fts5(statement,identity_text,content='search_doc',content_rowid='id',tokenize='unicode61');
CREATE TRIGGER search_ai AFTER INSERT ON search_doc BEGIN
 INSERT INTO search_fts(rowid,statement,identity_text) VALUES(new.id,new.statement,new.identity_text);
END;
CREATE TRIGGER search_ad AFTER DELETE ON search_doc BEGIN
 INSERT INTO search_fts(search_fts,rowid,statement,identity_text) VALUES('delete',old.id,old.statement,old.identity_text);
END;
CREATE TRIGGER search_au AFTER UPDATE ON search_doc BEGIN
 INSERT INTO search_fts(search_fts,rowid,statement,identity_text) VALUES('delete',old.id,old.statement,old.identity_text);
 INSERT INTO search_fts(rowid,statement,identity_text) VALUES(new.id,new.statement,new.identity_text);
END;
CREATE TABLE embedding_generation (
 id TEXT PRIMARY KEY, model TEXT NOT NULL, artifact_digest TEXT NOT NULL,
 dimensions INTEGER NOT NULL CHECK(dimensions>0),
 normalization TEXT NOT NULL, tokenizer_digest TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
) STRICT;
CREATE TABLE revision_embedding (
 project_id TEXT NOT NULL, revision_id TEXT NOT NULL,
 generation_id TEXT NOT NULL REFERENCES embedding_generation(id),
 dimensions INTEGER NOT NULL CHECK(dimensions>0), embedding BLOB NOT NULL,
 CHECK(length(embedding)=dimensions*4),
 FOREIGN KEY(project_id,revision_id) REFERENCES memory_revision(project_id,id),
 PRIMARY KEY(revision_id,generation_id)
) STRICT;
CREATE INDEX embedding_project ON revision_embedding(project_id,generation_id,revision_id);
CREATE TABLE ingest_checkpoint (
 project_id TEXT NOT NULL REFERENCES project(id), adapter_id TEXT NOT NULL,
 source_handle TEXT NOT NULL, local_checkpoint TEXT NOT NULL,
 generation TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(project_id,adapter_id,source_handle)
) STRICT;
CREATE TABLE work_item (
 id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(id),
 kind TEXT NOT NULL CHECK(kind IN ('embed','needs_agent','source_unavailable')),
 revision_id TEXT, source_ref_id TEXT, generation_id TEXT,
 dedup_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('pending','leased','done','failed')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 not_before TEXT NOT NULL, lease_until TEXT,
 FOREIGN KEY(project_id,revision_id) REFERENCES memory_revision(project_id,id),
 FOREIGN KEY(project_id,source_ref_id) REFERENCES source_ref(project_id,id),
 FOREIGN KEY(generation_id) REFERENCES embedding_generation(id),
 CHECK((kind='embed' AND revision_id IS NOT NULL AND generation_id IS NOT NULL)
    OR (kind IN ('needs_agent','source_unavailable') AND source_ref_id IS NOT NULL))
) STRICT;
CREATE INDEX work_ready ON work_item(state,not_before,id);
CREATE TABLE integration_state (
 project_id TEXT NOT NULL REFERENCES project(id), adapter_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('disabled','ready','syncing','cooldown','pending_reauth','error')),
 cooling_down_until TEXT, needs_reauth INTEGER NOT NULL CHECK(needs_reauth IN (0,1)),
 local_session_handle TEXT, last_success_at TEXT,
 PRIMARY KEY(project_id,adapter_id)
) STRICT;
CREATE TABLE file_discovery (
 project_id TEXT NOT NULL REFERENCES project(id), adapter_id TEXT NOT NULL,
 authorized_locator TEXT NOT NULL, file_generation TEXT NOT NULL,
 inode INTEGER, byte_size INTEGER NOT NULL CHECK(byte_size>=0),
 mtime_ns INTEGER, committed_offset INTEGER NOT NULL CHECK(committed_offset>=0),
 parser_generation TEXT NOT NULL, sanitized_digest TEXT,
 extraction_state TEXT NOT NULL CHECK(extraction_state IN ('discovered','needs_agent','recorded','unavailable')),
 PRIMARY KEY(project_id,adapter_id,authorized_locator)
) STRICT;
CREATE TRIGGER immutable_project_update BEFORE UPDATE ON project
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_project_delete BEFORE DELETE ON project
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_project_alias_update BEFORE UPDATE ON project_alias
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_project_alias_delete BEFORE DELETE ON project_alias
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_source_ref_update BEFORE UPDATE ON source_ref
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_source_ref_delete BEFORE DELETE ON source_ref
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_memory_update BEFORE UPDATE ON memory
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_memory_delete BEFORE DELETE ON memory
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_memory_revision_update BEFORE UPDATE ON memory_revision
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_memory_revision_delete BEFORE DELETE ON memory_revision
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_ledger_event_update BEFORE UPDATE ON ledger_event
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_ledger_event_delete BEFORE DELETE ON ledger_event
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_relation_event_update BEFORE UPDATE ON relation_event
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_relation_event_delete BEFORE DELETE ON relation_event
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_idempotency_receipt_update BEFORE UPDATE ON idempotency_receipt
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
CREATE TRIGGER immutable_idempotency_receipt_delete BEFORE DELETE ON idempotency_receipt
BEGIN SELECT RAISE(ABORT,'append-only authority'); END;
