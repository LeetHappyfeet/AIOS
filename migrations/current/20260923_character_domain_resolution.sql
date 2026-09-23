-- Character-side structured domain resolution.
-- Unknown source-native franchise/fandom/domain identifiers are staged, never inferred from prose.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_domain_candidate (
    candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    source_id uuid REFERENCES aios.character_identity_source(source_id) ON DELETE CASCADE,
    identifier_type text NOT NULL,
    identifier_value text NOT NULL,
    relationship text NOT NULL DEFAULT 'native'
        CHECK (relationship IN ('native','crossover')),
    status text NOT NULL DEFAULT 'unresolved'
        CHECK (status IN ('unresolved','resolved','ignored')),
    resolved_domain_id uuid REFERENCES aios.knowledge_domain(domain_id) ON DELETE SET NULL,
    source_field text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE (character_id, source_id, identifier_type, identifier_value, relationship)
);

CREATE INDEX IF NOT EXISTS idx_character_domain_candidate_unresolved
    ON aios.character_domain_candidate(identifier_type, identifier_value, status)
    WHERE status='unresolved';

COMMENT ON TABLE aios.character_domain_candidate IS
'Structured character-source domain affiliations that cannot yet resolve through the knowledge-domain registry. Prose is never inspected and unresolved rows do not alter identity or corpus access.';

COMMIT;
