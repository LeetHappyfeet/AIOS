-- Corpus catalog and source-profile routing.
-- Profiles classify trusted sources once; documents inherit collection, ACL scope,
-- namespace and identity binding without hand-written per-document taxonomy.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.corpus_collection (
    collection_key text PRIMARY KEY,
    display_name text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (collection_key <> '')
);

INSERT INTO aios.corpus_scope (scope_key, display_name, meta)
VALUES (
    'unclassified',
    'Unclassified Corpus',
    '{"catalog_fallback":true,"character_access_default":false}'::jsonb
)
ON CONFLICT (scope_key) DO NOTHING;

INSERT INTO aios.corpus_collection (collection_key, display_name, meta)
VALUES (
    'inbox',
    'Unclassified Corpus Inbox',
    '{"catalog_fallback":true}'::jsonb
)
ON CONFLICT (collection_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS aios.corpus_source_profile (
    profile_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    profile_key text NOT NULL UNIQUE,
    source_id text,
    domain_pattern text,
    collection_key text NOT NULL REFERENCES aios.corpus_collection(collection_key) ON DELETE RESTRICT,
    scope_key text NOT NULL REFERENCES aios.corpus_scope(scope_key) ON DELETE RESTRICT,
    epistemic_namespace text NOT NULL DEFAULT 'reference',
    identity_binding text NOT NULL DEFAULT 'external'
        CHECK (identity_binding IN ('external','world','character')),
    priority integer NOT NULL DEFAULT 0,
    enabled boolean NOT NULL DEFAULT true,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (profile_key <> ''),
    CHECK (source_id IS NOT NULL OR domain_pattern IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_corpus_source_profile_source
    ON aios.corpus_source_profile (source_id, enabled, priority DESC);
CREATE INDEX IF NOT EXISTS idx_corpus_source_profile_domain
    ON aios.corpus_source_profile (domain_pattern, enabled, priority DESC);

CREATE TABLE IF NOT EXISTS aios.corpus_document_collection (
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    collection_key text NOT NULL REFERENCES aios.corpus_collection(collection_key) ON DELETE CASCADE,
    assigned_by text NOT NULL DEFAULT 'catalog',
    profile_id uuid REFERENCES aios.corpus_source_profile(profile_id) ON DELETE SET NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, collection_key)
);

CREATE INDEX IF NOT EXISTS idx_corpus_document_collection_collection
    ON aios.corpus_document_collection (collection_key, document_id);

COMMENT ON TABLE aios.corpus_collection IS
'Administrative library/catalog grouping. Collections describe ingestion sets; character access remains controlled by corpus_scope.';
COMMENT ON TABLE aios.corpus_source_profile IS
'Trusted deterministic routing from source/domain to corpus collection, ACL scope, epistemic namespace and identity binding.';
COMMENT ON TABLE aios.corpus_document_collection IS
'Catalog membership for cold corpus documents. This is descriptive provenance, not character knowledge.';
COMMENT ON COLUMN aios.corpus_source_profile.domain_pattern IS
'Lowercase hostname or leading-dot suffix pattern (for example digimon.fandom.com or .example.org).';

COMMIT;
