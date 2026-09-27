-- Rename the action result mode "terminal" to "final".
-- "terminal" described lifecycle completion and is ambiguous with terminal/VM capabilities.
BEGIN;

ALTER TABLE aios.character_action
  DROP CONSTRAINT IF EXISTS character_action_result_mode_check;

ALTER TABLE aios.character_action
  ALTER COLUMN result_mode SET DEFAULT 'final';

UPDATE aios.character_action
SET result_mode = 'final'
WHERE result_mode = 'terminal';

ALTER TABLE aios.character_action
  ADD CONSTRAINT character_action_result_mode_check
  CHECK (result_mode IN ('final','return_to_cognition','asynchronous','external'));

COMMIT;
