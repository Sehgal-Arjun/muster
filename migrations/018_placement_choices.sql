-- Which folder a document belongs in, decided once and cached.
--
-- The deterministic arms are good at recall and poor at knowledge. A German
-- letter from the Studierendenwerk München about collecting keys belongs in
-- Housing/TUM, and getting there requires knowing that TUM is a university in
-- Munich — which no amount of word matching or folder-name similarity contains.
--
-- So the arms narrow it to a shortlist and this records one judgement over that
-- shortlist. Keyed by the document's text and the shortlist it was shown, because
-- a different shortlist is a different question.
CREATE TABLE placement_choices (
    text_key    TEXT NOT NULL,   -- hash of (file name, first page of text)
    options_key TEXT NOT NULL,   -- hash of the shortlist offered
    model_id    TEXT NOT NULL,
    chosen      TEXT,            -- folder path, or NULL for "none of these"
    reason      TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (text_key, options_key, model_id)
);
