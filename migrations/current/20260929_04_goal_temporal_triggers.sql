-- Goals with a future time window sleep until a deterministic temporal wake.
BEGIN;

ALTER TABLE aios.character_agent_goal
  DROP CONSTRAINT IF EXISTS character_agent_goal_status_check;

ALTER TABLE aios.character_agent_goal
  ADD CONSTRAINT character_agent_goal_status_check
  CHECK (status IN ('active','scheduled','dormant','completed','cancelled'));

ALTER TABLE aios.character_temporal_trigger
  ADD COLUMN IF NOT EXISTS goal_id uuid NULL
    REFERENCES aios.character_agent_goal(goal_id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS window_end_at timestamptz NULL,
  ADD COLUMN IF NOT EXISTS last_evaluated_at timestamptz NULL,
  ADD COLUMN IF NOT EXISTS last_outcome text NULL;

CREATE INDEX IF NOT EXISTS idx_character_temporal_trigger_goal
  ON aios.character_temporal_trigger(goal_id,status,due_at)
  WHERE goal_id IS NOT NULL;

COMMIT;
