-- Character corpus-domain provenance and identity projection support.
-- Domain affinity is not learned knowledge and corpus material never authors identity.
BEGIN;

ALTER TABLE aios.character_knowledge_domain
    ADD COLUMN IF NOT EXISTS relationship text NOT NULL DEFAULT 'granted',
    ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'operator',
    ADD COLUMN IF NOT EXISTS source_facet_id uuid
        REFERENCES aios.character_identity_facet(facet_id) ON DELETE SET NULL;

ALTER TABLE aios.character_knowledge_domain
    DROP CONSTRAINT IF EXISTS character_knowledge_domain_relationship_check;

ALTER TABLE aios.character_knowledge_domain
    ADD CONSTRAINT character_knowledge_domain_relationship_check
    CHECK (relationship IN ('native', 'crossover', 'study', 'granted'));

ALTER TABLE aios.character_knowledge_domain
    DROP CONSTRAINT IF EXISTS character_knowledge_domain_provenance_check;

ALTER TABLE aios.character_knowledge_domain
    ADD CONSTRAINT character_knowledge_domain_provenance_check
    CHECK (provenance IN ('identity_facet', 'operator', 'study', 'grant'));

CREATE INDEX IF NOT EXISTS idx_character_knowledge_domain_provenance
    ON aios.character_knowledge_domain (character_id, provenance, enabled);

CREATE INDEX IF NOT EXISTS idx_character_knowledge_domain_source_facet
    ON aios.character_knowledge_domain (source_facet_id)
    WHERE source_facet_id IS NOT NULL;

COMMENT ON TABLE aios.character_knowledge_domain IS
'Character corpus-domain affinity/eligibility. This is not learned knowledge. Identity-owned rows are projected one-way from authoritative character identity facets; corpus documents and search hits never author character identity.';

COMMENT ON COLUMN aios.character_knowledge_domain.relationship IS
'How the character relates to the information domain: native, crossover, study, or granted.';

COMMENT ON COLUMN aios.character_knowledge_domain.provenance IS
'Authority that created the domain relation. identity_facet is projected from durable authored identity; operator/study/grant remain independent.';

COMMENT ON COLUMN aios.character_knowledge_domain.source_facet_id IS
'Authoritative identity facet that produced an identity_facet domain row, when applicable.';

COMMIT;
