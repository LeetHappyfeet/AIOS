-- AIOS domain migration: epistemics and character/world runtime
-- Consolidates normalized propositions, character knowledge, concrete runtime
-- entities/state, epistemic weighting, character-root world topology, and
-- runtime perception cursors.

BEGIN;

-- AIOS epistemic control layer
-- 2026-09-05
--
-- Separates immutable observations from normalized propositions, keeps source
-- narratives distinct from possible-world branching, projects knowledge through
-- explicit acquisition events, and tracks generated gap-fill facts as provisional.


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
    source_entity_id uuid,
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
    source_entity_id uuid,
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

-- -------------------------------------------------

-- AIOS concrete world runtime kernel
-- 2026-09-05
--
-- Adds a shared runtime substrate beneath human users, digital characters,
-- autonomous agents, devices, vehicles, and nested/hosted actors.  Existing
-- epistemic world/claim tables remain authoritative for knowledge promotion.


ALTER TABLE aios.character_instance
    ADD COLUMN IF NOT EXISTS parent_instance_id uuid REFERENCES aios.character_instance(instance_id),
    ADD COLUMN IF NOT EXISTS forked_from_node_id uuid REFERENCES aios.dag_node(node_id);


CREATE TABLE IF NOT EXISTS aios.world_entity (
    entity_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    world_id uuid NOT NULL REFERENCES aios.world(world_id),
    entity_key text,
    entity_type text NOT NULL DEFAULT 'object',
    display_name text,
    character_instance_id uuid REFERENCES aios.character_instance(instance_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (world_id, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_world_entity_world_type
    ON aios.world_entity (world_id, entity_type);

CREATE TABLE IF NOT EXISTS aios.entity_controller (
    controller_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    entity_id uuid NOT NULL REFERENCES aios.world_entity(entity_id) ON DELETE CASCADE,
    controller_type text NOT NULL,
    controller_ref text NOT NULL,
    authority text NOT NULL DEFAULT 'primary',
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (entity_id, controller_type, controller_ref)
);

CREATE TABLE IF NOT EXISTS aios.world_entity_relation (
    relation_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    world_id uuid NOT NULL REFERENCES aios.world(world_id),
    subject_entity_id uuid NOT NULL REFERENCES aios.world_entity(entity_id),
    relation_type text NOT NULL,
    object_entity_id uuid NOT NULL REFERENCES aios.world_entity(entity_id),
    valid_from_node_id uuid REFERENCES aios.dag_node(node_id),
    valid_to_node_id uuid REFERENCES aios.dag_node(node_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (subject_entity_id <> object_entity_id)
);

CREATE INDEX IF NOT EXISTS idx_world_entity_relation_subject
    ON aios.world_entity_relation (world_id, subject_entity_id, relation_type)
    WHERE valid_to_node_id IS NULL;

CREATE TABLE IF NOT EXISTS aios.character_runtime_state (
    instance_id uuid PRIMARY KEY REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    world_id uuid NOT NULL REFERENCES aios.world(world_id),
    timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id),
    head_node_id uuid REFERENCES aios.dag_node(node_id),
    lifecycle_state text NOT NULL DEFAULT 'initializing',
    location_entity_id uuid REFERENCES aios.world_entity(entity_id),
    health double precision,
    stamina double precision,
    energy double precision,
    physical_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    emotional_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    social_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    goals jsonb NOT NULL DEFAULT '[]'::jsonb,
    active_tasks jsonb NOT NULL DEFAULT '[]'::jsonb,
    runtime_flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    state_version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_character_runtime_world
    ON aios.character_runtime_state (world_id, lifecycle_state);

CREATE TABLE IF NOT EXISTS aios.character_inventory (
    inventory_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    entity_id uuid NOT NULL REFERENCES aios.world_entity(entity_id),
    quantity double precision NOT NULL DEFAULT 1,
    equipped boolean NOT NULL DEFAULT false,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instance_id, entity_id)
);

CREATE TABLE IF NOT EXISTS aios.character_relationship (
    relationship_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    observer_instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    target_entity_id uuid NOT NULL REFERENCES aios.world_entity(entity_id),
    relationship_type text,
    affinity double precision,
    trust double precision,
    familiarity double precision,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (observer_instance_id, target_entity_id)
);

CREATE TABLE IF NOT EXISTS aios.character_knowledge (
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    claim_id uuid NOT NULL REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    epistemic_status text NOT NULL DEFAULT 'observed',
    confidence double precision,
    source_entity_id uuid REFERENCES aios.world_entity(entity_id),
    first_node_id uuid REFERENCES aios.dag_node(node_id),
    last_node_id uuid REFERENCES aios.dag_node(node_id),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, claim_id)
);

CREATE TABLE IF NOT EXISTS aios.world_rule (
    rule_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    rule_key text NOT NULL,
    rule_type text NOT NULL DEFAULT 'constraint',
    enabled boolean NOT NULL DEFAULT true,
    priority integer NOT NULL DEFAULT 100,
    rule_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (world_id, rule_key)
);

CREATE TABLE IF NOT EXISTS aios.world_event (
    world_event_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    world_id uuid NOT NULL REFERENCES aios.world(world_id),
    timeline_id uuid REFERENCES aios.timeline(timeline_id),
    instance_id uuid REFERENCES aios.character_instance(instance_id),
    actor_entity_id uuid REFERENCES aios.world_entity(entity_id),
    target_entity_id uuid REFERENCES aios.world_entity(entity_id),
    action_type text NOT NULL,
    status text NOT NULL DEFAULT 'accepted',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    dag_node_id uuid REFERENCES aios.dag_node(node_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_world_event_world_created
    ON aios.world_event (world_id, created_at DESC);

-- character_instance.world_id is its origin world; current_world_id is mutable.
UPDATE aios.character_instance
SET current_world_id = COALESCE(current_world_id, world_id)
WHERE current_world_id IS NULL;

-- -------------------------------------------------

-- Link epistemic character knowledge to concrete runtime entities.
-- Deliberately sorts after 20260905_world_runtime.sql.


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='knowledge_acquisition_source_entity_fkey'
          AND conrelid='aios.knowledge_acquisition_event'::regclass
    ) THEN
        ALTER TABLE aios.knowledge_acquisition_event
            ADD CONSTRAINT knowledge_acquisition_source_entity_fkey
            FOREIGN KEY (source_entity_id)
            REFERENCES aios.world_entity(entity_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='character_proposition_knowledge_source_entity_fkey'
          AND conrelid='aios.character_proposition_knowledge'::regclass
    ) THEN
        ALTER TABLE aios.character_proposition_knowledge
            ADD CONSTRAINT character_proposition_knowledge_source_entity_fkey
            FOREIGN KEY (source_entity_id)
            REFERENCES aios.world_entity(entity_id);
    END IF;
END $$;

-- -------------------------------------------------

CREATE TABLE IF NOT EXISTS aios.character_epistemic_profile (
    character_id text PRIMARY KEY REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    skepticism double precision NOT NULL DEFAULT 0.5 CHECK (skepticism BETWEEN 0 AND 1),
    curiosity double precision NOT NULL DEFAULT 0.5 CHECK (curiosity BETWEEN 0 AND 1),
    authority_trust double precision NOT NULL DEFAULT 0.5 CHECK (authority_trust BETWEEN 0 AND 1),
    novelty_seeking double precision NOT NULL DEFAULT 0.5 CHECK (novelty_seeking BETWEEN 0 AND 1),
    emotional_reactivity double precision NOT NULL DEFAULT 0.5 CHECK (emotional_reactivity BETWEEN 0 AND 1),
    retention double precision NOT NULL DEFAULT 0.7 CHECK (retention BETWEEN 0 AND 1),
    source_trust jsonb NOT NULL DEFAULT '{}'::jsonb,
    topic_interest jsonb NOT NULL DEFAULT '{}'::jsonb,
    domain_expertise jsonb NOT NULL DEFAULT '{}'::jsonb,
    trait_weights jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE aios.character_proposition_knowledge
    ADD COLUMN IF NOT EXISTS base_confidence double precision,
    ADD COLUMN IF NOT EXISTS attention_weight double precision,
    ADD COLUMN IF NOT EXISTS trust_weight double precision,
    ADD COLUMN IF NOT EXISTS compatibility_weight double precision,
    ADD COLUMN IF NOT EXISTS retention_weight double precision,
    ADD COLUMN IF NOT EXISTS salience_weight double precision,
    ADD COLUMN IF NOT EXISTS effective_confidence double precision;

-- -------------------------------------------------

-- Character-root and DAG-anchored world topology
-- 2026-09-06

ALTER TABLE aios.world
    ADD COLUMN IF NOT EXISTS root_world_id uuid REFERENCES aios.world(world_id),
    ADD COLUMN IF NOT EXISTS anchor_timeline_id uuid REFERENCES aios.timeline(timeline_id),
    ADD COLUMN IF NOT EXISTS anchor_node_id uuid REFERENCES aios.dag_node(node_id),
    ADD COLUMN IF NOT EXISTS origin_character_id text REFERENCES aios.character_identity(character_id);

CREATE INDEX IF NOT EXISTS idx_world_root_world_id
    ON aios.world(root_world_id);

CREATE INDEX IF NOT EXISTS idx_world_anchor_node_id
    ON aios.world(anchor_node_id);

CREATE INDEX IF NOT EXISTS idx_world_origin_character_id
    ON aios.world(origin_character_id);

CREATE TABLE IF NOT EXISTS aios.world_rdf_projection (
    world_id uuid PRIMARY KEY REFERENCES aios.world(world_id) ON DELETE CASCADE,
    rdf_graph text NOT NULL DEFAULT 'urn:aios:world:topology',
    projected_at timestamptz,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Backfill one stable personal-universe root for every existing character that
-- does not already have a home world.
INSERT INTO aios.world (
    world_key,
    world_type,
    origin_character_id,
    meta
)
SELECT
    'char:' || ci.character_id || ':root',
    'character_root',
    ci.character_id,
    jsonb_build_object(
        'source', 'character_identity',
        'topology_role', 'personal_universe_root'
    )
FROM aios.character_identity ci
WHERE ci.home_world_id IS NULL
ON CONFLICT (world_key) DO NOTHING;

UPDATE aios.world w
SET root_world_id = w.world_id
WHERE w.world_type = 'character_root'
  AND w.root_world_id IS NULL;

UPDATE aios.character_identity ci
SET home_world_id = w.world_id,
    updated_at = now()
FROM aios.world w
WHERE ci.home_world_id IS NULL
  AND w.world_key = 'char:' || ci.character_id || ':root';

-- -------------------------------------------------

-- Runtime source-perception cursors for HUD assembly
-- 2026-09-06

ALTER TABLE aios.character_runtime_state
    ADD COLUMN IF NOT EXISTS source_timeline_id uuid REFERENCES aios.timeline(timeline_id),
    ADD COLUMN IF NOT EXISTS source_head_node_id uuid REFERENCES aios.dag_node(node_id);

CREATE INDEX IF NOT EXISTS idx_character_runtime_source_timeline
    ON aios.character_runtime_state (source_timeline_id);

CREATE INDEX IF NOT EXISTS idx_character_runtime_source_head
    ON aios.character_runtime_state (source_head_node_id);

-- Backfill existing normal session runtimes from their authorized source
-- timeline.  The world anchor remains the immutable branch origin; the runtime
-- source head is the moving perception boundary and therefore starts at the
-- latest source node currently available. Forks are left conservative because
-- their perception boundary must be copied explicitly from their parent.
UPDATE aios.character_runtime_state rs
SET source_timeline_id = COALESCE(rs.source_timeline_id, w.anchor_timeline_id),
    source_head_node_id = COALESCE(
        rs.source_head_node_id,
        latest_source.node_id,
        w.anchor_node_id
    ),
    updated_at = now()
FROM aios.world w
JOIN aios.character_instance ci
  ON ci.current_world_id=w.world_id
LEFT JOIN LATERAL (
    SELECT dn.node_id
    FROM aios.dag_node dn
    WHERE dn.timeline_id=w.anchor_timeline_id
    ORDER BY dn.event_id DESC
    LIMIT 1
) latest_source ON true
WHERE w.world_id = rs.world_id
  AND ci.instance_id=rs.instance_id
  AND ci.parent_instance_id IS NULL
  AND (
      (rs.source_timeline_id IS NULL AND w.anchor_timeline_id IS NOT NULL)
      OR (rs.source_head_node_id IS NULL AND w.anchor_node_id IS NOT NULL)
  );

COMMIT;
