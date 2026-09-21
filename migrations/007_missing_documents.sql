-- Documents whose original has gone: deleted, trashed, or on a drive that is
-- not here.
--
-- Marked rather than removed, and marked rather than deleted on sight. Absence
-- is not proof of deletion: macOS denies access to Documents and Downloads
-- until a program is granted it, and a walk of a denied folder reports zero
-- files — which is indistinguishable from an empty one. Treating that as "the
-- user deleted everything" would erase an archive because of a permission
-- prompt. An unmounted volume does the same.
--
-- So the walk only ever marks documents under a root it actually read, and
-- removal stays a separate, deliberate act: `muster purge --missing`.
ALTER TABLE docs ADD COLUMN missing_since TEXT;

-- Retrieval filters on this on every query, and almost every row is null.
CREATE INDEX idx_docs_missing ON docs (missing_since) WHERE missing_since IS NOT NULL;
