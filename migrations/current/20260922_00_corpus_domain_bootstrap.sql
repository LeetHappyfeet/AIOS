-- Bootstrap relations required by the retained 20260922 corpus/domain migrations.
-- This migration is intentionally ordered before the same-day consumers.
-- It is additive for upgraded databases and establishes prerequisites for fresh installs.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_knowledge_domain (
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    knowledge_domain text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (character_id, knowledge_domain),
    CHECK (knowledge_domain <> '')
);

CREATE INDEX IF NOT EXISTS idx_character_knowledge_domain_domain
    ON aios.character_knowledge_domain (knowledge_domain, enabled, character_id);

CREATE TABLE IF NOT EXISTS aios.corpus_scope (
    scope_key text PRIMARY KEY,
    display_name text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (scope_key <> '')
);

INSERT INTO aios.corpus_scope (scope_key, display_name)
VALUES ('general', 'General')
ON CONFLICT (scope_key) DO NOTHING;

COMMIT;
