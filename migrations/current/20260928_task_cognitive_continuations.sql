BEGIN;
ALTER TABLE aios.character_cognitive_operation
  ADD COLUMN IF NOT EXISTS source_task_id uuid NULL
    REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS cognitive_operation_source_task_idx
  ON aios.character_cognitive_operation(source_task_id,status,created_at)
  WHERE source_task_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS aios.character_cognitive_task_dependency (
  dependency_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_task_id uuid NOT NULL REFERENCES aios.character_cognitive_task(task_id) ON DELETE CASCADE,
  child_task_id uuid NOT NULL REFERENCES aios.character_cognitive_task(task_id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','succeeded','failed','cancelled')),
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE(parent_task_id,child_task_id)
);
CREATE INDEX IF NOT EXISTS cognitive_task_dependency_parent_idx
  ON aios.character_cognitive_task_dependency(parent_task_id,status,created_at);
COMMIT;
