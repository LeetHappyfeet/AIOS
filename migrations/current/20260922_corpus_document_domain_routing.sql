-- Document-level domain routing from trusted structured corpus facets.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.corpus_facet_route (
    facet_type text NOT NULL,
    facet_value text NOT NULL,
    knowledge_domain text NOT NULL,
    scope_key text NOT NULL,
    epistemic_namespace text NOT NULL,
    access_class text NOT NULL DEFAULT 'domain'
        CHECK (access_class IN ('domain','restricted')),
    priority integer NOT NULL DEFAULT 0,
    enabled boolean NOT NULL DEFAULT true,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (facet_type, facet_value, knowledge_domain),
    CHECK (facet_type <> ''),
    CHECK (facet_value <> ''),
    CHECK (knowledge_domain <> ''),
    CHECK (scope_key <> ''),
    CHECK (epistemic_namespace <> '')
);

CREATE INDEX IF NOT EXISTS idx_corpus_facet_route_lookup
    ON aios.corpus_facet_route (facet_type, facet_value, enabled, priority DESC);

CREATE TABLE IF NOT EXISTS aios.corpus_document_domain (
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    knowledge_domain text NOT NULL,
    source text NOT NULL DEFAULT 'facet_route',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, knowledge_domain),
    CHECK (knowledge_domain <> '')
);

CREATE INDEX IF NOT EXISTS idx_corpus_document_domain_lookup
    ON aios.corpus_document_domain (knowledge_domain, document_id);

COMMENT ON TABLE aios.corpus_facet_route IS
'Trusted deterministic mapping from source-native document facets to AIOS document domains/scopes. Freeform prose never creates routes.';
COMMENT ON TABLE aios.corpus_document_domain IS
'Document-level logical domains used for domain corpus eligibility. This decouples heterogeneous repositories from source-profile domains.';

COMMIT;
