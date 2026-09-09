-- AIOS domain migration: semantic vector inference and topology
-- Consolidates semantic index state, topology, anchor edges, clustering,
-- classification, neighbor relations, reconciliation, and the current
-- Qdrant/proposition-leaf convergence repairs. This domain does not grant
-- world truth or character ownership; those remain explicit epistemic/runtime
-- promotion decisions.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_vector_index_state (
    object_type text NOT NULL,
    object_key text NOT NULL,
    qdrant_collection text NOT NULL,
    embedding_model text NOT NULL,
    embedding_version text NOT NULL,
    vector_hash text,
    indexed_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    PRIMARY KEY (
        object_type, object_key, qdrant_collection,
        embedding_model, embedding_version
    )
);

CREATE INDEX IF NOT EXISTS semantic_vector_index_state_lookup_idx
ON aios.semantic_vector_index_state (object_type, indexed_at);

CREATE TABLE IF NOT EXISTS aios.semantic_neighbor_candidate (
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    neighbor_proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    similarity double precision NOT NULL,
    relation_hint text NOT NULL DEFAULT 'semantic_neighbor',
    status text NOT NULL DEFAULT 'candidate',
    embedding_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (proposition_id <> neighbor_proposition_id),
    PRIMARY KEY (proposition_id, neighbor_proposition_id, embedding_version)
);

CREATE INDEX IF NOT EXISTS semantic_neighbor_candidate_score_idx
ON aios.semantic_neighbor_candidate (similarity DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_structure_state (
    proposition_id uuid PRIMARY KEY REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    embedding_version text NOT NULL,
    analyzed_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE aios.semantic_neighbor_candidate IS
'Advisory vector-neighbor candidates only. Never authoritative for truth, world membership, branch membership, equivalence, or epistemic visibility.';

-- -------------------------------------------------

-- Derived semantic topology for /char, /world and neutral source observations
-- 2026-09-06
--
-- This layer organizes already-derived observations/assertions into meaningful
-- branch points. It is not a truth store: source observations remain source
-- scoped, character topology requires character ownership, and world topology
-- requires an explicit world_proposition_assertion.


CREATE TABLE IF NOT EXISTS aios.semantic_topology_node (
    topology_node_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    scope_key text NOT NULL,
    scope_kind text NOT NULL CHECK (scope_kind IN ('character','world','source')),
    node_type text NOT NULL,
    node_key text NOT NULL,
    label text,
    character_id text REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    character_instance_id uuid REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    world_id uuid REFERENCES aios.world(world_id) ON DELETE CASCADE,
    source_id text REFERENCES aios.source_identity(source_id) ON DELETE CASCADE,
    timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    dag_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    proposition_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    claim_id uuid REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    assertion_id uuid REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE CASCADE,
    acquisition_id uuid REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE,
    significance double precision NOT NULL DEFAULT 0.5 CHECK (significance BETWEEN 0 AND 1),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope_key, node_type, node_key)
);

CREATE INDEX IF NOT EXISTS idx_semantic_topology_scope
    ON aios.semantic_topology_node (scope_key, node_type);
CREATE INDEX IF NOT EXISTS idx_semantic_topology_character
    ON aios.semantic_topology_node (character_id, created_at DESC)
    WHERE character_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_semantic_topology_world
    ON aios.semantic_topology_node (world_id, created_at DESC)
    WHERE world_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_semantic_topology_source
    ON aios.semantic_topology_node (source_id, created_at DESC)
    WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS aios.semantic_topology_edge (
    edge_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    scope_key text NOT NULL,
    parent_node_id uuid NOT NULL REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE CASCADE,
    child_node_id uuid NOT NULL REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE CASCADE,
    edge_type text NOT NULL,
    significance double precision NOT NULL DEFAULT 0.5 CHECK (significance BETWEEN 0 AND 1),
    claim_id uuid REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    assertion_id uuid REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE CASCADE,
    acquisition_id uuid REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_node_id <> child_node_id),
    UNIQUE (scope_key, parent_node_id, child_node_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_semantic_topology_edge_parent
    ON aios.semantic_topology_edge (scope_key, parent_node_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_semantic_topology_edge_child
    ON aios.semantic_topology_edge (scope_key, child_node_id, edge_type);

CREATE TABLE IF NOT EXISTS aios.semantic_topology_projection (
    projection_key text PRIMARY KEY,
    claim_id uuid REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    assertion_id uuid REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE CASCADE,
    acquisition_id uuid REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE,
    scope_key text NOT NULL,
    rdf_dataset text NOT NULL,
    rdf_graph text NOT NULL,
    resolver_version text NOT NULL,
    projected_at timestamptz,
    last_error text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (claim_id IS NOT NULL OR assertion_id IS NOT NULL OR acquisition_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_semantic_topology_projection_claim
    ON aios.semantic_topology_projection (claim_id)
    WHERE claim_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_semantic_topology_projection_assertion
    ON aios.semantic_topology_projection (assertion_id)
    WHERE assertion_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_semantic_topology_projection_acquisition
    ON aios.semantic_topology_projection (acquisition_id)
    WHERE acquisition_id IS NOT NULL;

-- -------------------------------------------------

-- Remove schema artifacts owned only by the retired monolithic RAG subsystem.
-- Qdrant data is external and is intentionally not deleted by a PostgreSQL migration.

DROP TABLE IF EXISTS aios.claim_world_affinity CASCADE;
DROP TABLE IF EXISTS aios.section_cluster_assignment CASCADE;
DROP TABLE IF EXISTS aios.world_split_candidate CASCADE;
DROP TABLE IF EXISTS aios.vector_index_state CASCADE;

-- -------------------------------------------------

-- Cross-scope semantic anchors between character memory topology and /world topology
-- 2026-09-06
--
-- Internal semantic_topology_edge rows describe structure within one scope.
-- semantic_anchor_edge describes typed references across scopes without
-- transferring epistemic ownership or world truth.


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

-- -------------------------------------------------

-- Advisory semantic clustering over proposition-neighbor geometry.
-- Clusters are structural candidates only; they do not create topics, branches,
-- worlds, truth assertions, or character knowledge.


CREATE TABLE IF NOT EXISTS aios.semantic_cluster_run (
    run_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    embedding_version text NOT NULL,
    algorithm_version text NOT NULL,
    core_threshold double precision NOT NULL,
    attach_threshold double precision NOT NULL,
    min_cluster_size integer NOT NULL,
    structure_watermark timestamptz,
    config_signature text NOT NULL,
    cluster_count integer NOT NULL DEFAULT 0,
    outlier_count integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL DEFAULT 'running',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_run_latest
    ON aios.semantic_cluster_run (embedding_version, completed_at DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_cluster_candidate (
    cluster_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    cluster_key uuid NOT NULL,
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    embedding_version text NOT NULL,
    algorithm_version text NOT NULL,
    member_count integer NOT NULL,
    internal_edge_count integer NOT NULL DEFAULT 0,
    density double precision NOT NULL DEFAULT 0.0,
    cohesion double precision NOT NULL DEFAULT 0.0,
    boundary_strength double precision NOT NULL DEFAULT 0.0,
    separation double precision NOT NULL DEFAULT 0.0,
    status text NOT NULL DEFAULT 'candidate',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, cluster_key)
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_candidate_key
    ON aios.semantic_cluster_candidate (cluster_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_semantic_cluster_candidate_run
    ON aios.semantic_cluster_candidate (run_id, cohesion DESC);
CREATE INDEX IF NOT EXISTS idx_semantic_cluster_candidate_status
    ON aios.semantic_cluster_candidate (embedding_version, status, cohesion DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_cluster_membership (
    cluster_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    membership_kind text NOT NULL CHECK (membership_kind IN ('core','fringe')),
    affinity double precision NOT NULL DEFAULT 0.0,
    internal_degree integer NOT NULL DEFAULT 0,
    strongest_neighbor_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    strongest_similarity double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, proposition_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_membership_proposition
    ON aios.semantic_cluster_membership (proposition_id, affinity DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_cluster_boundary (
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    cluster_a_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    cluster_b_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    edge_count integer NOT NULL,
    mean_similarity double precision NOT NULL,
    max_similarity double precision NOT NULL,
    min_similarity double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (cluster_a_id <> cluster_b_id),
    PRIMARY KEY (run_id, cluster_a_id, cluster_b_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_boundary_strength
    ON aios.semantic_cluster_boundary (run_id, max_similarity DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_outlier_candidate (
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    embedding_version text NOT NULL,
    reason text NOT NULL,
    nearest_similarity double precision,
    nearest_proposition_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'candidate',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, proposition_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_outlier_candidate_prop
    ON aios.semantic_outlier_candidate (proposition_id, created_at DESC);

COMMENT ON TABLE aios.semantic_cluster_candidate IS
'Advisory vector-geometry clusters. Classification into topic, state transition, narrative split, or branch requires later semantic/context validation.';

COMMENT ON TABLE aios.semantic_cluster_boundary IS
'Cross-cluster vector bridges retained for later topic/state/narrative/branch separation analysis.';

COMMENT ON TABLE aios.semantic_outlier_candidate IS
'Advisory semantic outliers. Never delete, reject, or demote propositions solely from this table.';

-- -------------------------------------------------

-- Semantic classification of cluster regions and boundaries.
-- Results are advisory structural interpretations only.


CREATE TABLE IF NOT EXISTS aios.semantic_boundary_classification (
    classification_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    cluster_a_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    cluster_b_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    classification text NOT NULL CHECK (
        classification IN (
            'SAME_REGION',
            'TOPIC_SPLIT',
            'TEMPORAL_TRANSITION',
            'STATE_TRANSITION',
            'NARRATIVE_SPLIT',
            'CONTRADICTION_CLUSTER',
            'EXPERIENTIAL_BRANCH_CANDIDATE',
            'WORLD_BRANCH_CANDIDATE',
            'UNRESOLVED'
        )
    ),
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    classifier_version text NOT NULL,
    status text NOT NULL DEFAULT 'candidate',
    feature_scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, cluster_a_id, cluster_b_id, classifier_version)
);

CREATE INDEX IF NOT EXISTS idx_semantic_boundary_classification_type
    ON aios.semantic_boundary_classification
       (classification, confidence DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_cluster_classification (
    classification_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    cluster_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    classification text NOT NULL CHECK (
        classification IN (
            'TOPIC_REGION',
            'STATE_SERIES',
            'EVENT_REGION',
            'MEMORY_REGION',
            'BELIEF_REGION',
            'RULE_REGION',
            'GOAL_REGION',
            'MIXED_REGION',
            'UNRESOLVED'
        )
    ),
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    classifier_version text NOT NULL,
    status text NOT NULL DEFAULT 'candidate',
    feature_scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, cluster_id, classifier_version)
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_classification_type
    ON aios.semantic_cluster_classification
       (classification, confidence DESC, created_at DESC);

COMMENT ON TABLE aios.semantic_boundary_classification IS
'Advisory interpretation of cluster boundaries. Branch candidate labels never create branches automatically.';

COMMENT ON TABLE aios.semantic_cluster_classification IS
'Advisory interpretation of semantic cluster contents for later topology routing and pruning.';

-- -------------------------------------------------

-- Pairwise semantic interpretation of Qdrant proposition neighbors.
-- These relations are advisory and may guide clustering/topology, but do not
-- replace proposition identity, conflicts, RDF, or epistemic authority.


CREATE TABLE IF NOT EXISTS aios.semantic_neighbor_relation (
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    neighbor_proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    embedding_version text NOT NULL,
    relation text NOT NULL CHECK (
        relation IN (
            'EQUIVALENT',
            'REFINES',
            'CONTRADICTS',
            'SAME_TOPIC',
            'SAME_EVENT',
            'RELATED',
            'UNRESOLVED'
        )
    ),
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    classifier_version text NOT NULL,
    status text NOT NULL DEFAULT 'candidate',
    features jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (proposition_id <> neighbor_proposition_id),
    PRIMARY KEY (
        proposition_id,
        neighbor_proposition_id,
        embedding_version,
        classifier_version
    )
);

CREATE INDEX IF NOT EXISTS idx_semantic_neighbor_relation_type
    ON aios.semantic_neighbor_relation
       (embedding_version, relation, confidence DESC);

COMMENT ON TABLE aios.semantic_neighbor_relation IS
'Advisory pairwise semantic interpretation of vector-neighbor propositions.';

-- -------------------------------------------------

-- Reconciliation/promotion layer for semantic vector inference.
-- Vector/classifier results may enrich topology and RDF, but never grant truth,
-- world membership, epistemic ownership, or create runtime branches directly.


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

-- -------------------------------------------------

-- Rebuild derived semantic topology after Qdrant/tree plumbing fixes.
-- 2026-09-08
--
-- This migration invalidates only derived semantic projections and classifier
-- receipts. It does not change proposition identity, source ownership, world
-- assertions, character knowledge, or any epistemic authority.


-- Existing acquisition projections were built before provenance-first
-- CHARACTER -> SOURCE anchoring. Existing world assertion projections need to
-- run again so SOURCE -> WORLD reports_about anchors can be backfilled.
UPDATE aios.semantic_topology_projection
SET projected_at=NULL,
    updated_at=now(),
    meta=meta || jsonb_build_object(
        'reproject_reason',
        'semantic_qdrant_tree_rebuild_20260908'
    )
WHERE (acquisition_id IS NOT NULL OR assertion_id IS NOT NULL)
  AND projected_at IS NOT NULL;

-- Earlier reconciliation materialized every accepted cluster as a disconnected
-- SEMANTIC_CLUSTER node. Remove only those receipt-owned derived nodes so the
-- reconciler can replace them with typed, structurally-parented pivots.
DELETE FROM aios.semantic_topology_node n
USING aios.semantic_reconciliation_receipt r
WHERE r.source_kind='cluster'
  AND r.action='materialize_semantic_cluster'
  AND r.topology_node_id=n.topology_node_id;

-- Boundary edges may have referenced the old generic cluster nodes. Their edge
-- rows cascade with those nodes; discard the derived receipts so boundaries can
-- be reconciled against the rebuilt pivots.
DELETE FROM aios.semantic_reconciliation_receipt
WHERE source_kind IN ('cluster','boundary');

UPDATE aios.semantic_cluster_classification
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

UPDATE aios.semantic_boundary_classification
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

-- -------------------------------------------------

-- Separate semantic TOPIC grouping nodes from proposition identity leaves.
-- 2026-09-09
--
-- Older topology stored proposition_id directly on a TOPIC node keyed only by
-- (scope_key, topic_key). Multiple propositions sharing a topic therefore
-- collapsed onto one topology node, preventing pairwise semantic relations
-- such as SAME_TOPIC, REFINES and CONTRADICTS from being materialized safely.


-- TOPIC is now a grouping/pivot node only. Proposition identity is represented
-- by a dedicated PROPOSITION child keyed by proposition_id.
UPDATE aios.semantic_topology_node
SET proposition_id=NULL,
    claim_id=NULL,
    assertion_id=NULL,
    acquisition_id=NULL,
    dag_node_id=NULL,
    meta=meta || jsonb_build_object(
        'semantic_role',
        'topic_group',
        'rebuild_reason',
        'semantic_proposition_leaves_20260909'
    ),
    updated_at=now()
WHERE node_type='TOPIC';

-- Remove only classifier-derived pivot nodes from previous reconciliation.
-- They will be recreated and linked to the new proposition leaves.
DELETE FROM aios.semantic_topology_node n
USING aios.semantic_reconciliation_receipt r
WHERE r.source_kind='cluster'
  AND r.topology_node_id=n.topology_node_id;

-- Pair/boundary/cluster receipts all target the old topology representation.
DELETE FROM aios.semantic_reconciliation_receipt;

UPDATE aios.semantic_neighbor_relation
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

UPDATE aios.semantic_cluster_classification
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

UPDATE aios.semantic_boundary_classification
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

-- Re-run deterministic topology derivation idempotently. Existing ROOT, INSTANCE,
-- branch and TOPIC nodes are reused; new PROPOSITION leaves are added beneath
-- their topic groups.
UPDATE aios.semantic_topology_projection
SET projected_at=NULL,
    updated_at=now(),
    meta=meta || jsonb_build_object(
        'reproject_reason',
        'semantic_proposition_leaves_20260909'
    )
WHERE projected_at IS NOT NULL;

COMMIT;
