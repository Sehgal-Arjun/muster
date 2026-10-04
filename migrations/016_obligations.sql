-- Whether a request is an obligation, decided once and cached.
--
-- The extractor's job is to find what a document asks; deciding whether being
-- asked means being obliged is a different question, and a harder one. An
-- institution's mail is mostly invitations, advice and surveys phrased in the
-- imperative — "join us for a webinar", "consider extending", "complete this
-- 4-minute survey" — and a list where nine of twelve entries are not owed is a
-- list nobody reads.
--
-- One narrow yes/no per distinct request, cached like satisfaction and for the
-- same reasons: the answer for a given wording does not change, and asking again
-- on every render would turn a query back into a generation. Versioned, because
-- the question is asked by a prompt and a better prompt is a different question.
CREATE TABLE obligations (
    request_key TEXT NOT NULL,     -- hash of the request text
    model_id    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    obliged     INTEGER NOT NULL,  -- 0 | 1
    reason      TEXT,              -- the model's one-line justification
    created_at  TEXT NOT NULL,
    PRIMARY KEY (request_key, model_id, version)
);
