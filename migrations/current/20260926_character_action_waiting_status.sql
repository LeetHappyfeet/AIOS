-- Extend the durable action lifecycle with an explicit waiting state.
-- Kept separate from the immutable lifecycle-creation migration.
BEGIN;

ALTER TABLE aios.character_action
    DROP CONSTRAINT IF EXISTS character_action_status_check;

ALTER TABLE aios.character_action
    ADD CONSTRAINT character_action_status_check
    CHECK (status IN (
        'proposed','validated','waiting','queued','running',
        'succeeded','failed','rejected','cancelled','timed_out'
    ));

COMMIT;
