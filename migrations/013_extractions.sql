-- One structured summary per document, cached.
--
-- Feeding 300 raw documents to a model is roughly 300k tokens. Extracting once
-- per document and caching the result turns that into an 18k-token timeline a
-- local model handles comfortably — and makes the expensive half idempotent, so
-- a crash halfway through a process costs nothing.
--
-- Keyed on (doc_id, extractor_version): a change to the prompt or the schema is
-- a version bump, which invalidates nothing and simply re-extracts. The model id
-- is recorded rather than keyed on, because two models at the same version should
-- not both be trusted at once — the version is the contract.
CREATE TABLE doc_extractions (
    doc_id            INTEGER NOT NULL REFERENCES docs (id) ON DELETE CASCADE,
    extractor_version INTEGER NOT NULL,
    model_id          TEXT NOT NULL,
    payload           TEXT NOT NULL,        -- validated JSON, schema below
    created_at        TEXT NOT NULL,
    PRIMARY KEY (doc_id, extractor_version)
);

-- Documents the extractor could not produce valid output for, after retries.
-- Recorded rather than silently skipped: "4 documents I could not account for"
-- is an honest answer and a silent omission is not.
CREATE TABLE extraction_failures (
    doc_id            INTEGER NOT NULL REFERENCES docs (id) ON DELETE CASCADE,
    extractor_version INTEGER NOT NULL,
    reason            TEXT NOT NULL,
    attempts          INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (doc_id, extractor_version)
);
