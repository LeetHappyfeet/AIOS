-- Shared corpus discovery and document facets.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.corpus_document_facet (
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    facet_type text NOT NULL,
    facet_value text NOT NULL,
    source text NOT NULL DEFAULT 'adapter',
    confidence double precision NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, facet_type, facet_value),
    CHECK (facet_type <> ''), CHECK (facet_value <> '')
);
CREATE INDEX IF NOT EXISTS idx_corpus_document_facet_lookup ON aios.corpus_document_facet (facet_type, facet_value, document_id);
COMMENT ON TABLE aios.corpus_document_facet IS 'Descriptive document classification for corpus routing/ranking. Facets never grant character access or create character knowledge.';
COMMENT ON TABLE aios.character_knowledge_domain IS 'Authored character domain affinity/expertise for corpus routing and ranking. It is not learned knowledge and does not grant corpus access.';
COMMENT ON COLUMN aios.corpus_source_profile.knowledge_domain IS 'Descriptive logical domain for corpus relevance/ranking, not authorization.';

-- Remove grants created by the superseded domain-as-authorization model.
-- Explicit character ACL rows are preserved.
DELETE FROM aios.character_corpus_access
WHERE meta->>'derived_from'='knowledge_domain';

COMMIT;
