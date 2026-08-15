-- 001_initial: core schema.
--
-- Design invariants encoded here:
--   * docs.origin separates captured from derived content. Every retrieval
--     query filters to 'captured' by default; nothing writes 'derived' yet.
--   * vectors is keyed on (chunk_id, model_id) so a new embedding model is an
--     insert alongside the old rows, never a wipe.
--   * chunks.text_hash lets re-embedding skip unchanged text.
--   * blobs are content-addressed on disk; the DB only ever holds paths.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE docs (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,                            -- 'gmail' | 'file' | 'outlook' | 'imap'
    source_uid   TEXT NOT NULL,                            -- provider message id or absolute path
    origin       TEXT NOT NULL DEFAULT 'captured'          -- 'captured' | 'derived'
                 CHECK (origin IN ('captured', 'derived')),
    content_hash TEXT NOT NULL,
    blob_path    TEXT,                                     -- null for docs with no binary original
    mime         TEXT,
    title        TEXT,
    doc_date     TEXT NOT NULL,                            -- ISO8601
    date_source  TEXT NOT NULL,                            -- 'header' | 'exif' | 'pdfmeta' | 'birthtime' | 'mtime'
    counterparty TEXT,                                     -- primary external address or domain
    thread_uid   TEXT,
    meta_json    TEXT,
    ingested_at  TEXT NOT NULL,
    UNIQUE (source, source_uid)
);

-- Retrieval always filters origin and usually orders or windows by date.
CREATE INDEX idx_docs_origin_date ON docs (origin, doc_date);
CREATE INDEX idx_docs_content_hash ON docs (content_hash);
CREATE INDEX idx_docs_thread ON docs (thread_uid) WHERE thread_uid IS NOT NULL;
CREATE INDEX idx_docs_counterparty ON docs (counterparty) WHERE counterparty IS NOT NULL;

CREATE TABLE chunks (
    id        INTEGER PRIMARY KEY,
    doc_id    INTEGER NOT NULL REFERENCES docs (id) ON DELETE CASCADE,
    ord       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    UNIQUE (doc_id, ord)
);

CREATE INDEX idx_chunks_text_hash ON chunks (text_hash);

-- External-content FTS5 index over chunks.text. The triggers below are the
-- only thing keeping it in sync; writers must not touch chunks_fts directly.
CREATE VIRTUAL TABLE chunks_fts USING fts5 (
    text,
    content = 'chunks',
    content_rowid = 'id',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE vectors (
    chunk_id  INTEGER NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    model_id  TEXT NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY (chunk_id, model_id)
);

-- `muster embed` scans for chunks lacking a vector for one model_id.
CREATE INDEX idx_vectors_model ON vectors (model_id);

CREATE TABLE attachments (
    id               INTEGER PRIMARY KEY,
    doc_id           INTEGER NOT NULL REFERENCES docs (id) ON DELETE CASCADE,
    blob_path        TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    filename         TEXT,
    mime             TEXT,
    extracted_doc_id INTEGER REFERENCES docs (id),         -- attachment as its own searchable doc
    UNIQUE (doc_id, content_hash, filename)                -- makes re-ingest of a message a no-op
);

CREATE INDEX idx_attachments_hash ON attachments (content_hash);
CREATE INDEX idx_attachments_extracted ON attachments (extracted_doc_id)
    WHERE extracted_doc_id IS NOT NULL;

CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    payload_json TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- The worker loop claims work with (status, kind) predicates.
CREATE INDEX idx_jobs_status_kind ON jobs (status, kind, id);

CREATE TABLE journal (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    verb        TEXT NOT NULL,
    target      TEXT,
    before_json TEXT,
    after_json  TEXT
);

CREATE INDEX idx_journal_run ON journal (run_id, id);
