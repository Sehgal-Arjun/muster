-- Open items the user has settled themselves.
--
-- Most obligations in a real process are discharged somewhere this index cannot
-- see — a web portal, a phone call, a counter. On one real process 53 of 66 open
-- items had no record of fulfilment anywhere in the archive, and an item that can
-- never be satisfied by evidence will sit in the list forever until the list
-- stops being read.
--
-- Stored as evidence, not as a delete. The item keeps its history and gains a
-- date and a verdict, so "I did this on the 4th" is a fact the reconstruction
-- carries rather than an absence it cannot explain. A dismissal is keyed on the
-- request text rather than the document, because the same requirement restated
-- in a reminder is one obligation and settling it settles both.
CREATE TABLE item_dismissals (
    process_id  INTEGER NOT NULL REFERENCES processes (id) ON DELETE CASCADE,
    item_key    TEXT NOT NULL,        -- normalised request text
    verdict     TEXT NOT NULL         -- 'done' | 'not_applicable'
                CHECK (verdict IN ('done', 'not_applicable')),
    note        TEXT,
    -- What the process looked like when the user settled it. If documents
    -- arrive afterwards that bear on the same requirement, the dismissal is
    -- shown as contradicted rather than silently kept or silently dropped.
    settled_at  TEXT NOT NULL,
    known_docs  INTEGER NOT NULL,     -- size of the gathered set at that moment
    PRIMARY KEY (process_id, item_key)
);
