-- Where each source got to, so the next sync can resume instead of re-listing.
--
-- Gmail expires history after roughly a week, so `cursor` is a hint that can go
-- stale rather than a fact that can be relied on. Every reader of it must have a
-- path for "the provider rejected this", which is why the date-query sync is not
-- going away — it is the fallback, not the old way of doing it.
--
-- Keyed by (source, account) because one machine can hold two mailboxes, and a
-- cursor from one is meaningless to the other.
CREATE TABLE sync_state (
    source     TEXT NOT NULL,                    -- 'gmail' | 'file' | ...
    account    TEXT NOT NULL DEFAULT '',         -- email address, or '' where the
                                                 -- source has no notion of one
    cursor     TEXT,                             -- Gmail historyId; null until known
    last_sync  TEXT,                             -- ISO8601 of the last completed run
    last_result TEXT,                            -- one line, for `muster status`
    PRIMARY KEY (source, account)
);
