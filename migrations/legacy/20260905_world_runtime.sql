-- AIOS concrete world runtime kernel
-- 2026-09-05
--
-- Adds a shared runtime substrate beneath human users, digital characters,
-- autonomous agents, devices, vehicles, and nested/hosted actors.  Existing
-- epistemic world/claim tables remain authoritative for knowledge promotion.

BEGIN;

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

COMMIT;
