-- 003: index the subject as its own weighted FTS5 column.
--
-- Subjects are the highest-signal field in email and were previously reachable
-- only when a body chunk happened to repeat them. A separate column lets BM25
-- weight them: bm25(chunks_fts, 1.0, 3.0), text then title.
--
-- Column order is (text, title) deliberately, so that weight tuple reads in the
-- order the ranking function takes it and the existing snippet(chunks_fts, 0, …)
-- calls keep pointing at the body.
--
-- FTS5 external-content tables read their columns from the content table by
-- name, so chunks has to carry the title. Denormalising it onto every chunk —
-- rather than only the first — means a subject query reaches a long document's
-- seventh chunk as readily as its first. The cost is that a title term's
-- document frequency counts once per chunk, which shifts IDF slightly and
-- shifts it the same way for every document.

ALTER TABLE chunks ADD COLUMN title TEXT;

UPDATE chunks
   SET title = (SELECT d.title FROM docs d WHERE d.id = chunks.doc_id);

DROP TRIGGER chunks_ai;
DROP TRIGGER chunks_ad;
DROP TRIGGER chunks_au;
DROP TABLE chunks_fts;

CREATE VIRTUAL TABLE chunks_fts USING fts5 (
    text,
    title,
    content = 'chunks',
    content_rowid = 'id',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, text, title) VALUES (new.id, new.text, new.title);
END;

CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text, title)
    VALUES ('delete', old.id, old.text, old.title);
END;

CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text, title)
    VALUES ('delete', old.id, old.text, old.title);
    INSERT INTO chunks_fts (rowid, text, title) VALUES (new.id, new.text, new.title);
END;

-- Repopulate from the content table now that the shape has changed.
INSERT INTO chunks_fts (chunks_fts) VALUES ('rebuild');
