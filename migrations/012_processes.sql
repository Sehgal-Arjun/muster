-- Processes: the thing a pile of documents is *about*.
--
-- A visa application is not 40 emails and 12 PDFs, it is one process with six
-- required documents, four of which have been sent. Search answers "find me the
-- thing"; this is the scaffolding for answering "what is the state of the thing".
--
-- An anchor is a confirmed identifier belonging to a process: a domain, an
-- address, a thread, a reference-number pattern, a folder, or one specific
-- document. Anchors are SUFFICIENT, never necessary — anything matching one is
-- in the process unconditionally, and documents also arrive from the seed search
-- alone. A visa email from a personal gmail address and a scan called
-- scan_047.pdf belong to the process without matching any anchor at all, so
-- requiring an anchor would quietly lose exactly the documents least likely to
-- look official.
CREATE TABLE processes (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,       -- stable handle used by CLI and API
    name       TEXT NOT NULL,              -- what the user called it
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE process_anchors (
    id         INTEGER PRIMARY KEY,
    process_id INTEGER NOT NULL REFERENCES processes (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (
                   kind IN ('domain', 'address', 'thread_uid',
                            'ref_pattern', 'folder', 'doc_id')
               ),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (process_id, kind, value)
);

CREATE INDEX idx_anchors_process ON process_anchors (process_id);
-- Expansion joins from an anchor value back to documents, so this is the hot
-- direction: "which processes claim this domain".
CREATE INDEX idx_anchors_value ON process_anchors (kind, value);
