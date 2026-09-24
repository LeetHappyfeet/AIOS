-- Transitive cognition provenance and delegated-task resume state.
BEGIN;
ALTER TABLE aios.character_cognitive_task
  ADD COLUMN IF NOT EXISTS source_action_id uuid NULL REFERENCES aios.character_action(action_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS root_task_id uuid NULL REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS resume_count integer NOT NULL DEFAULT 0 CHECK (resume_count >= 0);

ALTER TABLE aios.character_agent_goal
  ADD COLUMN IF NOT EXISTS source_action_id uuid NULL REFERENCES aios.character_action(action_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS root_task_id uuid NULL REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_character_cognitive_task_parent_status
 ON aios.character_cognitive_task(parent_task_id,status,created_at);
COMMIT;
