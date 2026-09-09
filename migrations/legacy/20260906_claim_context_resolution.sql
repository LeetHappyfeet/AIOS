-- AIOS claim context resolution layer
-- 2026-09-06
--
-- Adds durable, source-lineage-derived semantic context between liminal
-- structural classification and normalized proposition creation. Presence in
-- this table describes where an assertion came from; it does not make the
-- assertion true in a world or known by any other character.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.claim_context_resolution (
    claim_id uuid PRIMARY KEY
        REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,

    claim_kind text NOT NULL DEFAULT 'UNKNOWN',
    subject_kind text,
    object_kind text,
    predicate_family text NOT NULL DEFAULT 'UNKNOWN',

    -- Provenance identity is intentionally not an FK: ingest can observe a
    -- character_id before character discovery/registry materialization.
    origin_character_id text,
    character_instance_id uuid
        REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL,
    viewpoint_id text,
    world_id uuid REFERENCES aios.world(world_id) ON DELETE SET NULL,
    timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    dag_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,

    epistemic_scope text NOT NULL DEFAULT 'source',
    acquisition_mode text,

    subject_is_pivot boolean NOT NULL DEFAULT false,
    object_is_pivot boolean NOT NULL DEFAULT false,

    confidence double precision NOT NULL DEFAULT 0.0
        CHECK (confidence BETWEEN 0.0 AND 1.0),
    resolver_version text NOT NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claim_context_character
    ON aios.claim_context_resolution (origin_character_id, resolved_at DESC)
    WHERE origin_character_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_context_instance
    ON aios.claim_context_resolution (character_instance_id, resolved_at DESC)
    WHERE character_instance_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_context_world
    ON aios.claim_context_resolution (world_id, resolved_at DESC)
    WHERE world_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_context_semantics
    ON aios.claim_context_resolution (claim_kind, predicate_family);

COMMIT;
