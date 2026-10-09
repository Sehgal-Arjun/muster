-- Files moved by Muster, so a move can be undone.
--
-- Not needed for the index to stay correct: a file document's identity is its
-- path, so a move is recorded by updating that path in place — the same document
-- moved, rather than one deletion and one arrival. Without that update the
-- deletion sweep marks the old path gone and the next walk ingests the same
-- bytes at the new path as a second document.
--
-- This table exists for the other reason: Muster performed the move, so Muster
-- should be able to take it back. It is the only place in the system that writes
-- to the user's files, and an action that cannot be undone deserves to be.
CREATE TABLE file_moves (
    id         INTEGER PRIMARY KEY,
    doc_id     INTEGER REFERENCES docs (id) ON DELETE SET NULL,
    from_path  TEXT NOT NULL,
    to_path    TEXT NOT NULL,
    moved_at   TEXT NOT NULL,
    undone_at  TEXT
);

CREATE INDEX idx_file_moves_doc ON file_moves (doc_id);
