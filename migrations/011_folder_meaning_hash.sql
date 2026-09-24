-- The hash of the text a folder's vector belongs to, kept on the folder itself.
--
-- Vectors are keyed by text so a rebuilt map reuses them, but that leaves the
-- vector and the folder joined only through the text they share. Storing the
-- hash makes it an index lookup rather than a string comparison against a
-- reconstructed phrase.
ALTER TABLE folders ADD COLUMN meaning_hash TEXT;
CREATE INDEX idx_folders_meaning ON folders (meaning_hash);
