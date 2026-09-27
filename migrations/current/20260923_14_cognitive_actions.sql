-- Internal cognitive state and action execution semantics.
BEGIN;
ALTER TABLE aios.character_action
  ADD COLUMN IF NOT EXISTS result_mode text NOT NULL DEFAULT 'terminal'
    CHECK (result_mode IN ('terminal','return_to_cognition','asynchronous','external'));

CREATE TABLE IF NOT EXISTS aios.character_agent_goal (
  goal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
  source_task_id uuid NULL REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL,
  source_node_id uuid NULL REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  goal_text text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','cancelled')),
  priority integer NOT NULL DEFAULT 100,
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz NULL
);
CREATE INDEX IF NOT EXISTS idx_character_agent_goal_active
 ON aios.character_agent_goal(instance_id,status,priority,created_at);
COMMIT;
