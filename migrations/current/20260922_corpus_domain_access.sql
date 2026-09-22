-- Deterministic corpus domain eligibility and path-bounded source routing.
BEGIN;

ALTER TABLE aios.corpus_source_profile
    ADD COLUMN IF NOT EXISTS path_prefix text,
    ADD COLUMN IF NOT EXISTS knowledge_domain text;

ALTER TABLE aios.corpus_source_profile
    DROP CONSTRAINT IF EXISTS corpus_source_profile_source_id_domain_pattern_check;

ALTER TABLE aios.corpus_source_profile
    ADD CONSTRAINT corpus_source_profile_route_check
    CHECK (source_id IS NOT NULL OR domain_pattern IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_corpus_source_profile_knowledge_domain
    ON aios.corpus_source_profile (knowledge_domain, enabled)
    WHERE knowledge_domain IS NOT NULL;

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

COMMENT ON TABLE aios.character_knowledge_domain IS
'Authored character eligibility for corpus domains. This is not learned knowledge; it deterministically materializes corpus ACL grants.';
COMMENT ON COLUMN aios.corpus_source_profile.knowledge_domain IS
'Logical domain whose eligible characters receive this profile scope, for example fiction.kim-possible.';
COMMENT ON COLUMN aios.corpus_source_profile.path_prefix IS
'Optional URL path prefix required in addition to source/domain matching. Used to bound multi-franchise hosts.';

COMMIT;
