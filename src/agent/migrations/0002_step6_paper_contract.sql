BEGIN;

-- Step 6 paper contract: one hypothetical paper position per idea, one fill
-- row per (position, fill kind), and an explicit flag for funding periods that
-- have no asset_ctx data rather than a fabricated zero.

ALTER TABLE paper_positions
    ADD COLUMN funding_missing boolean NOT NULL DEFAULT false;

ALTER TABLE paper_positions
    ADD CONSTRAINT paper_positions_idea_id_key UNIQUE (idea_id);

ALTER TABLE paper_positions
    ADD CONSTRAINT paper_positions_status_check
        CHECK (status IN ('OPEN', 'CLOSED'));

ALTER TABLE paper_positions
    ADD CONSTRAINT paper_positions_direction_check
        CHECK (direction IN ('long', 'short'));

ALTER TABLE paper_positions
    ADD CONSTRAINT paper_positions_exit_reason_check
        CHECK (exit_reason IS NULL OR exit_reason IN ('target', 'stop', 'time_stop', 'halt_flatten'));

ALTER TABLE paper_positions
    ADD CONSTRAINT paper_positions_outcome_class_check
        CHECK (outcome_class IS NULL OR outcome_class IN (
            'target_hit', 'stop_hit', 'time_stop', 'halt_flatten', 'data_problem'
        ));

ALTER TABLE paper_fills
    ADD CONSTRAINT paper_fills_kind_check
        CHECK (kind IN ('PAPER_ENTRY', 'PAPER_STOP', 'PAPER_TARGET', 'PAPER_TIME_STOP', 'PAPER_HALT_FLATTEN'));

ALTER TABLE paper_fills
    ADD CONSTRAINT paper_fills_position_id_kind_key UNIQUE (position_id, kind);

CREATE INDEX paper_fills_position_id_idx
    ON paper_fills (position_id);

COMMIT;
