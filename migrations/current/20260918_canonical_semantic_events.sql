-- Canonical semantic event identity
-- 2026-09-18
--
-- SAME_EVENT is pairwise evidence for occurrence identity.  It must not become
-- an O(n^2) proposition-to-proposition topology.  Semantic events remain
-- epistemic/inferred and are deliberately separate from aios.world_event,
-- which is reserved for causally admitted objective history.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_event (
    semantic_event_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    world_id uuid REFERENCES aios.world(world_id) ON DELETE CASCADE,
    timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    dag_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','merge_candidate','superseded')),
    confidence double precision NOT NULL DEFAULT 0.5
        CHECK (confidence BETWEEN 0.0 AND 1.0),
    resolver_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_semantic_event_coordinate
    ON aios.semantic_event (world_id, timeline_id, dag_node_id, status);

CREATE TABLE IF NOT EXISTS aios.semantic_event_membership (
    semantic_event_id uuid NOT NULL
        REFERENCES aios.semantic_event(semantic_event_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL
        REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    observation_id uuid REFERENCES aios.observation(observation_id) ON DELETE SET NULL,
    claim_id uuid REFERENCES aios.claim_candidate(claim_id) ON DELETE SET NULL,
    membership_confidence double precision NOT NULL
        CHECK (membership_confidence BETWEEN 0.0 AND 1.0),
    assigned_by text NOT NULL,
    evidence_source_id text,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','candidate','superseded')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (semantic_event_id, proposition_id)
);

-- A proposition may describe only one active canonical semantic occurrence.
CREATE UNIQUE INDEX IF NOT EXISTS ux_semantic_event_membership_active_proposition
    ON aios.semantic_event_membership (proposition_id)
    WHERE status='active';

CREATE INDEX IF NOT EXISTS idx_semantic_event_membership_event
    ON aios.semantic_event_membership (semantic_event_id, status);

CREATE TABLE IF NOT EXISTS aios.semantic_event_merge_candidate (
    event_a_id uuid NOT NULL
        REFERENCES aios.semantic_event(semantic_event_id) ON DELETE CASCADE,
    event_b_id uuid NOT NULL
        REFERENCES aios.semantic_event(semantic_event_id) ON DELETE CASCADE,
    source_id text NOT NULL,
    confidence double precision NOT NULL
        CHECK (confidence BETWEEN 0.0 AND 1.0),
    status text NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate','accepted','rejected','stale')),
    resolver_version text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (event_a_id <> event_b_id),
    PRIMARY KEY (event_a_id, event_b_id, source_id)
);

COMMENT ON TABLE aios.semantic_event IS
'Canonical inferred occurrence identity. Epistemic only; never implies causal admission or objective world truth.';
COMMENT ON TABLE aios.semantic_event_membership IS
'Proposition/observation evidence attached to a canonical semantic occurrence.';
COMMENT ON TABLE aios.semantic_event_merge_candidate IS
'Non-transitive merge evidence between already distinct semantic events; never auto-merges events.';

-- Remove the obsolete pairwise topology materialization. Pairwise SAME_EVENT
-- rows remain as classifier evidence and will be reconciled into semantic_event.
DELETE FROM aios.semantic_topology_edge
WHERE edge_type='semantic_same_event'
  AND inference_source='semantic_vector_classifier';

DELETE FROM aios.semantic_reconciliation_receipt
WHERE source_kind='neighbor_relation'
  AND action='semantic_same_event';

UPDATE aios.semantic_neighbor_relation
SET status='candidate', updated_at=now()
WHERE relation='SAME_EVENT'
  AND status='reconciled';

COMMIT;
