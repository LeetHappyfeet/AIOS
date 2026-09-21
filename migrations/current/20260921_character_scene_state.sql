-- AIOS character scene-state projection
--
-- Scene state is a character-instance-owned materialization of the effective
-- working scene at one pair of runtime/source DAG coordinates. It is not world
-- truth and never merges sibling character instances.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_scene_snapshot (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    runtime_timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    runtime_head_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    source_timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    source_head_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    parent_snapshot_id uuid REFERENCES aios.character_scene_snapshot(snapshot_id) ON DELETE SET NULL,
    scene_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_node_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
    projection_version text NOT NULL DEFAULT 'character-scene-v1',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (
        instance_id,
        runtime_timeline_id,
        runtime_head_node_id,
        source_timeline_id,
        source_head_node_id,
        projection_version
    )
);

CREATE INDEX IF NOT EXISTS idx_character_scene_snapshot_instance
    ON aios.character_scene_snapshot (instance_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_character_scene_snapshot_source
    ON aios.character_scene_snapshot (source_timeline_id, source_head_node_id)
    WHERE source_timeline_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS aios.character_scene_transition (
    transition_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    snapshot_id uuid NOT NULL REFERENCES aios.character_scene_snapshot(snapshot_id) ON DELETE CASCADE,
    runtime_timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    runtime_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    source_timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    source_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    slot_key text NOT NULL,
    before_value jsonb,
    after_value jsonb,
    persistence text NOT NULL DEFAULT 'until_changed',
    confidence double precision NOT NULL DEFAULT 1.0,
    evidence_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'active',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (persistence IN ('turn','until_changed','scene','session')),
    CHECK (status IN ('active','superseded','invalidated')),
    UNIQUE (snapshot_id, slot_key)
);

CREATE INDEX IF NOT EXISTS idx_character_scene_transition_instance
    ON aios.character_scene_transition (instance_id, created_at DESC);

COMMENT ON TABLE aios.character_scene_snapshot IS
'Character-relative working scene materialized at runtime/source DAG coordinates. Ownership is instance_id; sibling timelines must never be merged.';
COMMENT ON TABLE aios.character_scene_transition IS
'Auditable per-slot changes used to derive a character scene snapshot. Scene transitions are working state, not durable character knowledge or objective world truth.';

COMMIT;
