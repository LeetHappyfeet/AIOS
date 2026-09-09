-- Reconciliation/promotion layer for semantic vector inference.
-- Vector/classifier results may enrich topology and RDF, but never grant truth,
-- world membership, epistemic ownership, or create runtime branches directly.

BEGIN;

ALTER TABLE aios.semantic_topology_edge
    ADD COLUMN IF NOT EXISTS inference_source text NOT NULL DEFAULT 'deterministic',
    ADD COLUMN IF NOT EXISTS inference_status text NOT NULL DEFAULT 'accepted',
    ADD COLUMN IF NOT EXISTS inference_confidence double precision;

ALTER TABLE aios.semantic_topology_edge
    DROP CONSTRAINT IF EXISTS semantic_topology_edge_inference_confidence_check;

ALTER TABLE aios.semantic_topology_edge
    ADD CONSTRAINT semantic_topology_edge_inference_confidence_check
    CHECK (
        inference_confidence IS NULL
        OR (inference_confidence BETWEEN 0 AND 1)
    );

CREATE INDEX IF NOT EXISTS idx_semantic_topology_edge_inference
    ON aios.semantic_topology_edge
       (scope_key, inference_source, inference_status, edge_type);

CREATE TABLE IF NOT EXISTS aios.semantic_reconciliation_receipt (
    receipt_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    receipt_key text NOT NULL UNIQUE,
    source_kind text NOT NULL CHECK (
        source_kind IN ('neighbor_relation','cluster','boundary')
    ),
    source_id text NOT NULL,
    scope_key text NOT NULL,
    scope_partition_key text NOT NULL,
    action text NOT NULL,
    topology_node_id uuid REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE SET NULL,
    topology_edge_id uuid REFERENCES aios.semantic_topology_edge(edge_id) ON DELETE SET NULL,
    rdf_dataset text,
    rdf_graph text,
    classifier_version text,
    confidence double precision CHECK (
        confidence IS NULL OR confidence BETWEEN 0 AND 1
    ),
    status text NOT NULL DEFAULT 'accepted',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    reconciled_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_semantic_reconciliation_scope
    ON aios.semantic_reconciliation_receipt
       (scope_partition_key, source_kind, reconciled_at DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_branch_candidate (
    branch_candidate_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    boundary_classification_id uuid NOT NULL
        REFERENCES aios.semantic_boundary_classification(classification_id)
        ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    scope_key text NOT NULL,
    scope_partition_key text NOT NULL,
    scope_kind text NOT NULL,
    candidate_kind text NOT NULL CHECK (
        candidate_kind IN ('experiential','world')
    ),
    cluster_a_id uuid NOT NULL
        REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    cluster_b_id uuid NOT NULL
        REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    character_id text REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    character_instance_id uuid REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL,
    world_id uuid REFERENCES aios.world(world_id) ON DELETE SET NULL,
    timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status text NOT NULL DEFAULT 'candidate',
    reason jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (boundary_classification_id, scope_partition_key, candidate_kind)
);

CREATE INDEX IF NOT EXISTS idx_semantic_branch_candidate_status
    ON aios.semantic_branch_candidate (candidate_kind, status, confidence DESC);

COMMENT ON TABLE aios.semantic_branch_candidate IS
'Classifier-derived branch proposals only. Runtime/world creation requires a separate authoritative promotion decision.';

COMMIT;
