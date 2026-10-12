-- A better name for a file, proposed once and cached.
--
-- "Key Collection Template.pdf" is what the sender called it, and says nothing
-- about whose keys or which building. Naming is the other half of filing: a file
-- in the right folder under a meaningless name is still lost, just in a smaller
-- haystack.
--
-- Suggested only. The rename happens when a person confirms it, in the same
-- action as the move, and the original name is what stays if they do nothing.
CREATE TABLE name_suggestions (
    text_key   TEXT NOT NULL,   -- hash of (current name, first page of text)
    model_id   TEXT NOT NULL,
    version    INTEGER NOT NULL,
    suggested  TEXT,            -- basename without extension, or NULL for "it is fine"
    created_at TEXT NOT NULL,
    PRIMARY KEY (text_key, model_id, version)
);
