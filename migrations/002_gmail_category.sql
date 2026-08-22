-- 002: Gmail category as a first-class, indexable column.
--
-- The full label list lives in docs.meta_json, which is where arbitrary
-- provider metadata belongs. But retrieval now excludes promotions and social
-- by default, and a default filter runs on every single query — evaluating
-- json_extract over the candidate set each time would make the common path pay
-- for the rare one. This column is derived from those labels at ingest and
-- backfilled by `muster reparse`.
--
-- NULL means "not categorised": everything from non-Gmail sources, and Gmail
-- mail with no CATEGORY_* label (which is ordinary personal correspondence).
-- The default filter must therefore always let NULL through.

ALTER TABLE docs ADD COLUMN category TEXT;

CREATE INDEX idx_docs_category ON docs (category, doc_date)
    WHERE category IS NOT NULL;
