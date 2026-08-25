-- 005: make the people on a message searchable.
--
-- "Mira Halvorsen" and "Tomas Lindqvist" found nothing. Both names appear in the
-- archive exactly once each, in the From display name — "Mira Halvorsen
-- <m.halvorsen@university.example>" — and nowhere in any subject or body. The address
-- alone would not have helped: unicode61 reads mhalvorsen and tlindqvist as
-- single tokens, so neither matches the two-word query a person actually types.
--
-- So the display name is what has to be indexed, alongside the addresses for
-- the cases where the useful token is a domain rather than a name.
--
-- Same shape as 003: a denormalised column on chunks, its own FTS column,
-- separately weighted. Column order stays (text, title, party) so existing
-- bm25 and snippet call sites keep pointing at what they already point at.

ALTER TABLE docs ADD COLUMN sender_name TEXT;
ALTER TABLE chunks ADD COLUMN party TEXT;

-- Addresses can be backfilled from what is already stored. Display names
-- cannot — they were never kept — so `muster reparse` fills those in.
UPDATE chunks
   SET party = (
        SELECT trim(
                 coalesce(d.sender, '') || ' ' ||
                 CASE WHEN d.counterparty IS NOT d.sender
                      THEN coalesce(d.counterparty, '') ELSE '' END
               )
        FROM docs d WHERE d.id = chunks.doc_id
   );

DROP TRIGGER chunks_ai;
DROP TRIGGER chunks_ad;
DROP TRIGGER chunks_au;
DROP TABLE chunks_fts;

CREATE VIRTUAL TABLE chunks_fts USING fts5 (
    text,
    title,
    party,
    content = 'chunks',
    content_rowid = 'id',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, text, title, party)
    VALUES (new.id, new.text, new.title, new.party);
END;

CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text, title, party)
    VALUES ('delete', old.id, old.text, old.title, old.party);
END;

CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text, title, party)
    VALUES ('delete', old.id, old.text, old.title, old.party);
    INSERT INTO chunks_fts (rowid, text, title, party)
    VALUES (new.id, new.text, new.title, new.party);
END;

INSERT INTO chunks_fts (chunks_fts) VALUES ('rebuild');
