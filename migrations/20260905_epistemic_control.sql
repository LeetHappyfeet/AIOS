-- AIOS epistemic control layer
-- 2026-09-05
--
-- Separates immutable observations from normalized propositions, keeps source
-- narratives distinct from possible-world branching, projects knowledge through
-- explicit acquisition events, and tracks generated gap-fill facts as provisional.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.proposition (
    proposition_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    proposition_hash text NOT NULL UNIQUE,
    topic_key text NOT NULL,
    subject_norm text,
    predicate_norm text,
    object_norm text,
    polarity smallint NOT NULL DEFAULT 1 CHECK (polarity IN (-1, 1)),
    canonical_text text NOT NULL,
    modality text NOT NULL DEFAULT 'asserted',
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_proposition_topic
    ON aios.proposition (topic_key);

CREATE TABLE IF NOT EXISTS aios.observation (
    observation_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    claim_id uuid NOT NULL UNIQUE REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    document_id uuid REFERENCES aios.source_document(document_id),
    timeline_id uuid REFERENCES aios.timeline(timeline_id),
    dag_node_id uuid REFERENCES aios.dag_node(node_id),
    source_key text,
    source_domain text,
    source_kind text NOT NULL DEFAULT 'observed',
    observed_at timestamptz NOT NULL DEFAULT now(),
    extraction_confidence double precision,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_observation_proposition
    ON aios.observation (proposition_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observation_source
    ON aios.observation (source_domain, observed_at DESC);

CREATE TABLE IF NOT EXISTS aios.proposition_evidence (
    evidence_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    observation_id uuid REFERENCES aios.observation(observation_id) ON DELETE CASCADE,
    evidence_role text NOT NULL DEFAULT 'support',
    source_weight double precision NOT NULL DEFAULT 0.5,
    confidence double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (proposition_id, observation_id, evidence_role)
);

CREATE TABLE IF NOT EXISTS aios.proposition_conflict (
    conflict_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    topic_key text NOT NULL,
    proposition_a_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    proposition_b_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    conflict_type text NOT NULL,
    strength double precision NOT NULL DEFAULT 1.0,
    detected_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (proposition_a_id <> proposition_b_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_proposition_conflict_pair
    ON aios.proposition_conflict (
        LEAST(proposition_a_id, proposition_b_id),
        GREATEST(proposition_a_id, proposition_b_id),
        conflict_type
    );

CREATE TABLE IF NOT EXISTS aios.narrative_cluster (
    narrative_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    topic_key text NOT NULL,
    narrative_key text NOT NULL,
    label text,
    summary text,
    confidence double precision NOT NULL DEFAULT 0.5,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (topic_key, narrative_key)
);

CREATE TABLE IF NOT EXISTS aios.narrative_membership (
    narrative_id uuid NOT NULL REFERENCES aios.narrative_cluster(narrative_id) ON DELETE CASCADE,
    observation_id uuid NOT NULL REFERENCES aios.observation(observation_id) ON DELETE CASCADE,
    affinity double precision NOT NULL DEFAULT 1.0,
    assigned_by text NOT NULL DEFAULT 'deterministic-v1',
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (narrative_id, observation_id)
);

CREATE INDEX IF NOT EXISTS idx_narrative_membership_observation
    ON aios.narrative_membership (observation_id);

CREATE TABLE IF NOT EXISTS aios.narrative_source_affinity (
    narrative_id uuid NOT NULL REFERENCES aios.narrative_cluster(narrative_id) ON DELETE CASCADE,
    source_key text NOT NULL,
    observation_count integer NOT NULL DEFAULT 0,
    affinity double precision NOT NULL DEFAULT 0.0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (narrative_id, source_key)
);

CREATE TABLE IF NOT EXISTS aios.knowledge_acquisition_event (
    acquisition_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    proposition_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    claim_id uuid REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    acquisition_mode text NOT NULL,
    epistemic_status text NOT NULL DEFAULT 'observed',
    confidence double precision,
    source_entity_id uuid REFERENCES aios.world_entity(entity_id),
    dag_node_id uuid REFERENCES aios.dag_node(node_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (proposition_id IS NOT NULL OR claim_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_acquisition_pending
    ON aios.knowledge_acquisition_event (created_at)
    WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS aios.character_proposition_knowledge (
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    epistemic_status text NOT NULL DEFAULT 'observed',
    confidence double precision,
    acquisition_mode text NOT NULL,
    source_entity_id uuid REFERENCES aios.world_entity(entity_id),
    first_node_id uuid REFERENCES aios.dag_node(node_id),
    last_node_id uuid REFERENCES aios.dag_node(node_id),
    first_acquired_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (instance_id, proposition_id)
);

CREATE TABLE IF NOT EXISTS aios.world_proposition_assertion (
    assertion_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    epistemic_status text NOT NULL DEFAULT 'tentative',
    source_kind text NOT NULL DEFAULT 'observed',
    confidence double precision NOT NULL DEFAULT 0.5,
    generated_at_node_id uuid REFERENCES aios.dag_node(node_id),
    reason text,
    superseded_by_assertion_id uuid REFERENCES aios.world_proposition_assertion(assertion_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_checked_at timestamptz,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (world_id, proposition_id, source_kind)
);

CREATE INDEX IF NOT EXISTS idx_world_proposition_status
    ON aios.world_proposition_assertion (world_id, epistemic_status, source_kind);

COMMIT;
