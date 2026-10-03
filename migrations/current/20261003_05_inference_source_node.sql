-- Separate DAG source coordinates from character_cognitive_task foreign keys.
-- Existing task_id retains its original cognitive task semantics.
BEGIN;
ALTER TABLE aios.inference_request
    ADD COLUMN IF NOT EXISTS source_node_id uuid NULL
    REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_inference_request_source_node
    ON aios.inference_request (instance_id, worker_class, source_node_id, created_at DESC)
    WHERE source_node_id IS NOT NULL;
COMMIT;
