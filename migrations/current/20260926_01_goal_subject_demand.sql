BEGIN;

ALTER TABLE aios.character_cognitive_subject
  ADD COLUMN IF NOT EXISTS goal_id uuid
  REFERENCES aios.character_agent_goal(goal_id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_cognitive_subject_goal
  ON aios.character_cognitive_subject(instance_id,goal_id,status,last_seen_at DESC)
  WHERE goal_id IS NOT NULL;

COMMIT;
