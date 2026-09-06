BEGIN;

-- Step 8: the code-only baseline must stay comparable to the code+LLM book
-- (spec 11.5 SYSTEM RULE, 14.2). code_decision_before_llm records what the
-- deterministic pipeline decided before any model was consulted.

ALTER TABLE ideas
    ADD COLUMN code_decision_before_llm text;

ALTER TABLE ideas
    ADD COLUMN code_would_take boolean NOT NULL DEFAULT false;

ALTER TABLE ideas
    ADD COLUMN llm_involved boolean NOT NULL DEFAULT false;

-- spec 14.2 offers either paper_positions_shadow or a book flag; the flag is
-- the simpler of the two options the specification allows.
ALTER TABLE paper_positions
    ADD COLUMN book text NOT NULL DEFAULT 'live';

ALTER TABLE paper_positions
    ADD CONSTRAINT paper_positions_book_check
        CHECK (book IN ('live', 'shadow'));

CREATE INDEX ideas_llm_involved_idx
    ON ideas (llm_involved, setup_id, decision);

COMMIT;
