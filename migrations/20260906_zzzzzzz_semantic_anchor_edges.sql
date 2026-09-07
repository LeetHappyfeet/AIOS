-- Cross-scope semantic anchors between character memory topology and /world topology
-- 2026-09-06
--
-- Internal semantic_topology_edge rows describe structure within one scope.
-- semantic_anchor_edge describes typed references across scopes without
-- transferring epistemic ownership or world truth.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_anchor_edge (
    anchor_edge_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    source_scope_key text NOT NULL,
    source_node_id uuid NOT NULL
        REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE CASCADE,
    target_scope_key text NOT NULL,
    target_node_id uuid NOT NULL
        REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE CASCADE,
    relationship_type text NOT NULL,
    character_id text
        REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    character_instance_id uuid
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    world_id uuid
        REFERENCES aios.world(world_id) ON DELETE CASCADE,
    proposition_id uuid
        REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    acquisition_id uuid
        REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE,
    dag_node_id uuid
        REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    confidence double precision NOT NULL DEFAULT 1.0
        CHECK (confidence BETWEEN 0 AND 1),
    inference_source text NOT NULL DEFAULT 'deterministic',
    inference_status text NOT NULL DEFAULT 'accepted',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (source_node_id <> target_node_id),
    CHECK (source_scope_key <> target_scope_key),
    UNIQUE (source_node_id, target_node_id, relationship_type)
);

CREATE INDEX IF NOT EXISTS idx_semantic_anchor_source
    ON aios.semantic_anchor_edge (source_scope_key, relationship_type, source_node_id);
CREATE INDEX IF NOT EXISTS idx_semantic_anchor_target
    ON aios.semantic_anchor_edge (target_scope_key, relationship_type, target_node_id);
CREATE INDEX IF NOT EXISTS idx_semantic_anchor_character
    ON aios.semantic_anchor_edge (character_id, character_instance_id)
    WHERE character_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_semantic_anchor_world
    ON aios.semantic_anchor_edge (world_id, proposition_id)
    WHERE world_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_semantic_anchor_acquisition
    ON aios.semantic_anchor_edge (acquisition_id)
    WHERE acquisition_id IS NOT NULL;

-- Existing acquisition topology was projected before acquisition_id was
-- persisted on topology nodes and before cross-scope anchors existed.
-- Mark only those projections stale; the normal supervisor will rebuild them
-- idempotently and reproject the /char RDF graphs with deterministic anchors.
UPDATE aios.semantic_topology_projection
SET projected_at=NULL,
    updated_at=now(),
    meta=meta || jsonb_build_object('reproject_reason','semantic_anchor_backfill')
WHERE acquisition_id IS NOT NULL
  AND projected_at IS NOT NULL;

COMMIT;
