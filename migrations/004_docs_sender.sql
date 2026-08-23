-- 004: the sender address as a first-class, indexable column.
--
-- It was already in meta_json, but three things now need it cheaply:
-- filtering excluded senders out of retrieval, purging mail that was indexed
-- before its sender was excluded, and `muster status --senders`, which groups
-- the whole index by sender. Doing any of those through json_extract means a
-- full scan every time.
--
-- This is the sender specifically, not `counterparty`. They coincide for
-- inbound mail but diverge for anything sent, where counterparty is the
-- recipient — and "who sent this" is the question being asked here.
--
-- Backfilled from meta_json for rows that predate the column; `muster reparse`
-- sets it from the parsed headers thereafter.

ALTER TABLE docs ADD COLUMN sender TEXT;

UPDATE docs
   SET sender = lower(json_extract(meta_json, '$.from[0]'))
 WHERE meta_json IS NOT NULL
   AND json_valid(meta_json)
   AND json_extract(meta_json, '$.from[0]') IS NOT NULL;

CREATE INDEX idx_docs_sender ON docs (sender) WHERE sender IS NOT NULL;
