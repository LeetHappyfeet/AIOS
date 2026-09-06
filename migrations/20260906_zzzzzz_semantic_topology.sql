-- Derived semantic topology for /char, /world and neutral source observations
-- 2026-09-06
--
-- This layer organizes already-derived observations/assertions into meaningful
-- branch points. It is not a truth store: source observations remain source
-- scoped, character topology requires character ownership, and world topology
-- requires an explicit world_proposition_assertion.

BEGIN;

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

COMMIT;
