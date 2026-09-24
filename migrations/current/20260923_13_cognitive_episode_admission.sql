-- Cognitive episode provenance and lazy admission controls.
BEGIN;
ALTER TABLE aios.character_cognitive_task
  ADD COLUMN IF NOT EXISTS source_from_node_id uuid NULL REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS source_through_node_id uuid NULL REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS source_scene_snapshot_id uuid NULL REFERENCES aios.character_scene_snapshot(snapshot_id) ON DELETE SET NULL;
ALTER TABLE aios.character_agent_runtime
  ADD COLUMN IF NOT EXISTS last_cognitive_source_node_id uuid NULL REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS pending_cognitive_source_node_id uuid NULL REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS pending_cognitive_event_count integer NOT NULL DEFAULT 0 CHECK (pending_cognitive_event_count >= 0),
  ADD COLUMN IF NOT EXISTS cognitive_batch_size integer NOT NULL DEFAULT 4 CHECK (cognitive_batch_size BETWEEN 1 AND 128);
CREATE INDEX IF NOT EXISTS idx_character_cognitive_task_source_interval
  ON aios.character_cognitive_task(instance_id, source_from_node_id, source_through_node_id);
COMMIT;
