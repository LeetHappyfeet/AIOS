-- Replay validation resolves nodes from an indexed ingest-event key. The
-- timeline/event uniqueness index cannot serve an event-only lookup efficiently.
CREATE INDEX IF NOT EXISTS idx_dag_node_event_replay
    ON aios.dag_node (event_id, created_at, node_id) INCLUDE (timeline_id);
