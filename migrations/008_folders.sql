-- Directories, so a search can offer to take you to one.
--
-- Not documents: they have no content, never reach the chunk index, and must
-- never compete with real results for a place in the list. A folder is an
-- answer to a different question — "where does this live" rather than "what
-- did it say" — and is offered separately or not at all.
--
-- Names only. Recording what is inside a directory is what the file connector
-- already does; this is a map, not a second index.
CREATE TABLE folders (
    path      TEXT PRIMARY KEY,          -- absolute
    name      TEXT NOT NULL,             -- basename, for matching
    depth     INTEGER NOT NULL,          -- from its root; shallower ranks higher
    mtime     TEXT,                      -- recently touched folders rank higher
    seen_at   TEXT NOT NULL
);

CREATE INDEX idx_folders_name ON folders (name);
