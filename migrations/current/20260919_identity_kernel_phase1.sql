-- Phase 1 durable identity kernel.
-- Character identity is slow-changing authored configuration, not cognition or runtime state.
BEGIN;

ALTER TABLE aios.character_identity
    ADD COLUMN IF NOT EXISTS identity_version bigint NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS aios.character_identity_source (
    source_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    source_type text NOT NULL,
    source_format text,
    source_name text,
    source_hash text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    authority text NOT NULL DEFAULT 'authored',
    imported_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (character_id, source_hash)
);

CREATE INDEX IF NOT EXISTS idx_character_identity_source_character
    ON aios.character_identity_source(character_id, imported_at DESC);

CREATE TABLE IF NOT EXISTS aios.character_identity_facet (
    facet_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    facet_type text NOT NULL,
    facet_key text NOT NULL,
    value jsonb NOT NULL,
    stability text NOT NULL DEFAULT 'core'
        CHECK (stability IN ('structural','constitutional','core','developmental')),
    authority text NOT NULL DEFAULT 'authored',
    mutability text NOT NULL DEFAULT 'explicit'
        CHECK (mutability IN ('locked','explicit','developmental')),
    source_id uuid REFERENCES aios.character_identity_source(source_id) ON DELETE SET NULL,
    source_field text,
    source_fragment text,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded','rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (character_id, facet_type, facet_key)
);

CREATE INDEX IF NOT EXISTS idx_character_identity_facet_active
    ON aios.character_identity_facet(character_id, facet_type, facet_key)
    WHERE status='active';

CREATE TABLE IF NOT EXISTS aios.compiled_identity_kernel (
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    identity_version bigint NOT NULL,
    compiler_version text NOT NULL,
    kernel_json jsonb NOT NULL,
    kernel_text text NOT NULL,
    compiled_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (character_id, identity_version, compiler_version)
);

COMMENT ON TABLE aios.character_identity_source IS
'Immutable provenance for authored/imported identity material such as character cards. It is not conversational memory.';
COMMENT ON TABLE aios.character_identity_facet IS
'Accepted slow-changing identity definition. Ordinary cognition/runtime paths have no write authority here.';
COMMENT ON TABLE aios.compiled_identity_kernel IS
'Deterministic runtime projection of accepted identity facets, shared by all active instances of a character/version.';
COMMENT ON COLUMN aios.character_identity.identity_version IS
'Monotonic identity revision used to invalidate compiled kernels without coupling identity to runtime state.';

COMMIT;
