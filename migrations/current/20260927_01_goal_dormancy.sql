-- Add a non-terminal parked state for intentions that no longer deserve
-- executive attention but may become relevant again.
BEGIN;

ALTER TABLE aios.character_agent_goal
  DROP CONSTRAINT IF EXISTS character_agent_goal_status_check;

ALTER TABLE aios.character_agent_goal
  ADD CONSTRAINT character_agent_goal_status_check
  CHECK (status IN ('active','dormant','completed','cancelled'));

COMMIT;
