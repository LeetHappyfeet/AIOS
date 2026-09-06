-- Character HUD profiles and durable identity context
-- 2026-09-06
BEGIN;

ALTER TABLE aios.ingest_event
    ADD COLUMN IF NOT EXISTS viewpoint_id text;

ALTER TABLE aios.dag_node
    ADD COLUMN IF NOT EXISTS viewpoint_id text;

ALTER TABLE aios.claim_context_resolution
    ADD COLUMN IF NOT EXISTS speaker_id text,
    ADD COLUMN IF NOT EXISTS speaker_type text;

-- Preserve already-ingested explicit viewpoints where they were carried only
-- in JSON payload. Otherwise resolve deterministic defaults once.
UPDATE aios.ingest_event
SET viewpoint_id = COALESCE(
    NULLIF(payload->>'viewpoint_id',''),
    CASE
        WHEN speaker_role::text = 'character' THEN COALESCE(speaker_id, character_id)
        ELSE speaker_id
    END
)
WHERE viewpoint_id IS NULL;

UPDATE aios.dag_node
SET viewpoint_id = COALESCE(
    NULLIF(payload->>'viewpoint_id',''),
    CASE
        WHEN speaker_role::text = 'character' THEN COALESCE(speaker_id, character_id)
        ELSE speaker_id
    END
)
WHERE viewpoint_id IS NULL;

CREATE TABLE IF NOT EXISTS aios.hud_profile (
    profile_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    profile_name text NOT NULL UNIQUE,
    description text,

    token_budget integer NOT NULL DEFAULT 1600 CHECK (token_budget > 0),
    recent_event_limit integer NOT NULL DEFAULT 12 CHECK (recent_event_limit >= 0),
    memory_budget integer NOT NULL DEFAULT 350 CHECK (memory_budget >= 0),
    belief_budget integer NOT NULL DEFAULT 320 CHECK (belief_budget >= 0),
    relationship_budget integer NOT NULL DEFAULT 160 CHECK (relationship_budget >= 0),
    scene_budget integer NOT NULL DEFAULT 260 CHECK (scene_budget >= 0),
    inventory_budget integer NOT NULL DEFAULT 140 CHECK (inventory_budget >= 0),
    rules_budget integer NOT NULL DEFAULT 140 CHECK (rules_budget >= 0),
    goals_budget integer NOT NULL DEFAULT 140 CHECK (goals_budget >= 0),

    entity_hops integer NOT NULL DEFAULT 1 CHECK (entity_hops BETWEEN 0 AND 4),
    semantic_retrieval_limit integer NOT NULL DEFAULT 25 CHECK (semantic_retrieval_limit >= 0),
    deep_memory_limit integer NOT NULL DEFAULT 0 CHECK (deep_memory_limit >= 0),

    include_emotional_state boolean NOT NULL DEFAULT true,
    include_physical_state boolean NOT NULL DEFAULT true,
    include_social_state boolean NOT NULL DEFAULT true,
    include_inventory boolean NOT NULL DEFAULT true,
    include_relationships boolean NOT NULL DEFAULT true,
    include_conflicts boolean NOT NULL DEFAULT true,
    include_provenance boolean NOT NULL DEFAULT true,
    include_confidence boolean NOT NULL DEFAULT true,

    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aios.character_hud_profile (
    character_id text PRIMARY KEY
        REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    profile_id uuid NOT NULL
        REFERENCES aios.hud_profile(profile_id) ON DELETE RESTRICT,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO aios.hud_profile (profile_name, description)
VALUES (
    'default',
    'Default deterministic AIOS HUD profile.'
)
ON CONFLICT (profile_name) DO NOTHING;

INSERT INTO aios.character_hud_profile (character_id, profile_id)
SELECT ci.character_id, hp.profile_id
FROM aios.character_identity ci
JOIN aios.hud_profile hp ON hp.profile_name='default'
ON CONFLICT (character_id) DO NOTHING;

COMMIT;
