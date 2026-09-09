-- First-class external observation provenance boundaries
-- 2026-09-06
--
-- External sources are observations, not characters and not world truth.
-- This migration gives future accumulators a durable source identity and
-- explicit target hints while preserving liminal-first ingestion.

BEGIN;

ALTER TYPE aios.actor_type
    ADD VALUE IF NOT EXISTS 'source';

ALTER TYPE aios.event_kind
    ADD VALUE IF NOT EXISTS 'observation';

CREATE TABLE IF NOT EXISTS aios.source_identity (
    source_id text PRIMARY KEY,
    source_kind text NOT NULL,
    display_name text,
    canonical_uri text,
    canonical_domain text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_identity_domain
    ON aios.source_identity (canonical_domain)
    WHERE canonical_domain IS NOT NULL;

ALTER TABLE aios.ingest_event
    ADD COLUMN IF NOT EXISTS source_id text
        REFERENCES aios.source_identity(source_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source_kind text,
    ADD COLUMN IF NOT EXISTS target_character_id text,
    ADD COLUMN IF NOT EXISTS target_world_id uuid
        REFERENCES aios.world(world_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS provenance_version text
        NOT NULL DEFAULT 'provenance-v1';

ALTER TABLE aios.timeline
    ALTER COLUMN user_name DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS source_id text
        REFERENCES aios.source_identity(source_id) ON DELETE SET NULL;

ALTER TABLE aios.claim_context_resolution
    ADD COLUMN IF NOT EXISTS source_id text
        REFERENCES aios.source_identity(source_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source_kind text,
    ADD COLUMN IF NOT EXISTS target_character_id text,
    ADD COLUMN IF NOT EXISTS target_world_id uuid
        REFERENCES aios.world(world_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_ingest_event_source_identity
    ON aios.ingest_event (source_id, created_at DESC)
    WHERE source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ingest_event_target_character
    ON aios.ingest_event (target_character_id, created_at DESC)
    WHERE target_character_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ingest_event_target_world
    ON aios.ingest_event (target_world_id, created_at DESC)
    WHERE target_world_id IS NOT NULL;

DROP INDEX IF EXISTS aios.ux_timeline_identity;

CREATE UNIQUE INDEX ux_timeline_identity
    ON aios.timeline (
        world_id,
        name,
        session_id,
        character_id,
        user_name,
        scope_key,
        source_id
    ) NULLS NOT DISTINCT;

CREATE INDEX IF NOT EXISTS idx_timeline_source_identity
    ON aios.timeline (source_id, created_at DESC)
    WHERE source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_context_source
    ON aios.claim_context_resolution (source_id, resolved_at DESC)
    WHERE source_id IS NOT NULL;

COMMIT;
