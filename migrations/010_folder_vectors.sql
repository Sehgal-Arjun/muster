-- Meaning for folder names.
--
-- Word matching cannot know that a "Sample Test" is a "Sample Exams". FTS5 sees
-- two different strings; a reader sees the same folder. The index already
-- answers that for documents by running a second, semantic arm alongside the
-- keyword one and fusing the two, and a folder name is short enough that the
-- same treatment costs almost nothing.
--
-- Keyed by the hash of the text rather than by folder id, because the map is
-- rebuilt wholesale on every walk and re-embedding a thousand unchanged names
-- each time would be work for nothing. Same reasoning as chunks: a vector
-- belongs to a piece of text, not to the row that happens to hold it.
CREATE TABLE folder_vectors (
    text_hash TEXT NOT NULL,
    model_id  TEXT NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY (text_hash, model_id)
);
