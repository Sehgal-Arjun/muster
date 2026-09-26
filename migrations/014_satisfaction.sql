-- Whether one thing satisfies another, decided once and cached.
--
-- Open items are a set difference over cached extractions: a request with no
-- matching fulfilment is outstanding. That part is ordinary SQL — deterministic,
-- testable, and never generated.
--
-- The model's only job is the semantic match: does AOK_confirmation.pdf satisfy
-- "proof of health insurance"? One pair at a time, a small judgement made
-- reliably. Cached because the answer for a given pair cannot change, and
-- because asking again for every render would make the whole thing a generation
-- rather than a query.
CREATE TABLE satisfaction (
    request_key TEXT NOT NULL,    -- hash of (process, request text)
    candidate_key TEXT NOT NULL,  -- hash of (candidate text)
    model_id    TEXT NOT NULL,
    satisfied   INTEGER NOT NULL, -- 0 | 1
    reason      TEXT,             -- the model's one-line justification
    created_at  TEXT NOT NULL,
    PRIMARY KEY (request_key, candidate_key, model_id)
);
