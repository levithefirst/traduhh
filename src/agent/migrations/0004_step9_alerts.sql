BEGIN;

-- Step 9: durable alert de-duplication. A worker restart mid-dispatch must
-- not re-send an alert that already went out, so the dedupe key is derived
-- from the event itself (idea/position id + kind) and inserted before send.

CREATE TABLE alerts_sent (
    dedupe_key text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('trade_paper', 'paper_fill', 'paper_close', 'wait')),
    idea_id uuid,
    position_id uuid,
    chat_id bigint NOT NULL,
    sent_at timestamptz NOT NULL,
    delivered boolean NOT NULL DEFAULT false
);

CREATE INDEX alerts_sent_idea_id_idx ON alerts_sent (idea_id);
CREATE INDEX alerts_sent_position_id_idx ON alerts_sent (position_id);

COMMIT;
