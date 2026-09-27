-- Rename the original character_action result mode from terminal to final.
-- Kept separate from the immutable cognitive-actions migration.
BEGIN;

ALTER TABLE aios.character_action
    DROP CONSTRAINT IF EXISTS character_action_result_mode_check;

UPDATE aios.character_action
SET result_mode = 'final'
WHERE result_mode = 'terminal';

ALTER TABLE aios.character_action
    ALTER COLUMN result_mode SET DEFAULT 'final';

ALTER TABLE aios.character_action
    ADD CONSTRAINT character_action_result_mode_check
    CHECK (result_mode IN ('final','return_to_cognition','asynchronous','external'));

COMMIT;
