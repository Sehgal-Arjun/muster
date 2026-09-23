-- Make folders findable the way people actually type.
--
-- Matching the basename against a contiguous substring failed three ways at
-- once. "MATH213" is one word on disk and "math 213" is two as typed, so it
-- never matched. Only the folder's own name was searched, so "sample exams"
-- could not be reached by naming the course it sits under. And the query had to
-- be a substring in order, so remembering the words but not the phrasing was no
-- help at all.
--
-- FTS5 answers all three: tokens rather than substrings, prefix matching for
-- half-typed words, and BM25 to rank. The searchable text carries every segment
-- of the path plus the pieces each segment splits into, so MATH213 is findable
-- as "math" and "213" as well as whole.
DROP TABLE IF EXISTS folders;

CREATE TABLE folders (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,        -- absolute
    name      TEXT NOT NULL,               -- basename
    name_text TEXT NOT NULL,               -- basename, split into words
    path_text TEXT NOT NULL,               -- ancestors, split into words
    depth     INTEGER NOT NULL,
    mtime     TEXT,
    seen_at   TEXT NOT NULL
);

CREATE INDEX idx_folders_name ON folders (name);

-- External content: the map is rebuilt wholesale on every walk, so there is
-- nothing to keep in step incrementally and no triggers to get wrong.
CREATE VIRTUAL TABLE folders_fts USING fts5 (
    name_text,
    path_text,
    content = 'folders',
    content_rowid = 'id',
    tokenize = 'unicode61'
);
