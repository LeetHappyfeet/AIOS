-- Live HUD generation readiness and prepared snapshots
-- 2026-09-06

BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_hud_readiness (
    instance_id uuid PRIMARY KEY
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    source_timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    source_head_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    source_head_event_id bigint,
    retrieval_ready_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    retrieval_ready_event_id bigint,
    prepared_source_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    prepared_source_event_id bigint,
    prepared_state_version bigint,
    status text NOT NULL DEFAULT 'dirty'
        CHECK (status IN ('cold','dirty','preparing','ready','error')),
    live boolean NOT NULL DEFAULT false,
    dirty_since timestamptz,
    prepared_at timestamptz,
    last_error text,
    hud_json jsonb,
    hud_text text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_character_hud_readiness_live
    ON aios.character_hud_readiness (live, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_character_hud_readiness_source
    ON aios.character_hud_readiness (source_timeline_id, source_head_event_id);

COMMIT;
