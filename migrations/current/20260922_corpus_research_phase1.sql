-- Deterministic corpus research phase 1.
-- Character corpus access is a hard, character-level boundary. Search happens
-- inside the allowed corpus; search/exposure alone never creates /char knowledge.
BEGIN;

ALTER TABLE aios.corpus_section
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(heading, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_corpus_section_search_vector
    ON aios.corpus_section USING gin (search_vector);

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

CREATE TABLE IF NOT EXISTS aios.corpus_document_scope (
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    scope_key text NOT NULL REFERENCES aios.corpus_scope(scope_key) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, scope_key)
);

INSERT INTO aios.corpus_document_scope (document_id, scope_key)
SELECT document_id, 'general'
FROM aios.corpus_document
ON CONFLICT DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_corpus_document_scope_scope
    ON aios.corpus_document_scope (scope_key, document_id);

CREATE TABLE IF NOT EXISTS aios.character_corpus_access (
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    scope_key text NOT NULL REFERENCES aios.corpus_scope(scope_key) ON DELETE CASCADE,
    allowed boolean NOT NULL DEFAULT true,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (character_id, scope_key)
);

CREATE INDEX IF NOT EXISTS idx_character_corpus_access_character
    ON aios.character_corpus_access (character_id, allowed, scope_key);

CREATE TABLE IF NOT EXISTS aios.character_research_event (
    research_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    query_text text NOT NULL,
    query_terms text[] NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'searched'
        CHECK (status IN ('searched','no_access','no_results','skipped')),
    result_count integer NOT NULL DEFAULT 0,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_character_research_event_instance
    ON aios.character_research_event (instance_id, created_at DESC);

CREATE TABLE IF NOT EXISTS aios.character_corpus_exposure (
    research_id uuid NOT NULL REFERENCES aios.character_research_event(research_id) ON DELETE CASCADE,
    section_id uuid NOT NULL REFERENCES aios.corpus_section(section_id) ON DELETE CASCADE,
    rank integer NOT NULL,
    score double precision NOT NULL,
    exposed_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    acquisition_status text NOT NULL DEFAULT 'reference'
        CHECK (acquisition_status IN ('reference','eligible','acquired','rejected')),
    acquisition_score double precision,
    acquisition_reason text,
    consumption_id uuid REFERENCES aios.source_consumption(consumption_id) ON DELETE SET NULL,
    evaluated_at timestamptz,
    PRIMARY KEY (research_id, section_id)
);

CREATE INDEX IF NOT EXISTS idx_character_corpus_exposure_learning
    ON aios.character_corpus_exposure (acquisition_status, exposed_at DESC);

COMMENT ON TABLE aios.character_corpus_access IS
'Hard character-level corpus ACL. No grant means no corpus search access. Parent grants include descendant dotted scopes; matching denies override allows.';
COMMENT ON TABLE aios.character_research_event IS
'Deterministic corpus search audit. A search event is not a knowledge acquisition event.';
COMMENT ON TABLE aios.character_corpus_exposure IS
'Corpus sections returned as temporary reference context. Exposure does not imply belief or durable character knowledge.';

COMMIT;
