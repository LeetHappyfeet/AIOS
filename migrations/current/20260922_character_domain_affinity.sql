-- First-class character domain affinity with authoritative identity provenance.
BEGIN;

ALTER TABLE aios.character_knowledge_domain
    ADD COLUMN IF NOT EXISTS relationship text NOT NULL DEFAULT 'granted',
    ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'legacy_configuration',
    ADD COLUMN IF NOT EXISTS source_facet_id uuid REFERENCES aios.character_identity_facet(facet_id) ON DELETE SET NULL;

ALTER TABLE aios.character_knowledge_domain
    DROP CONSTRAINT IF EXISTS character_knowledge_domain_relationship_check;
ALTER TABLE aios.character_knowledge_domain
    ADD CONSTRAINT character_knowledge_domain_relationship_check
    CHECK (relationship IN ('native','crossover','acquired','granted'));

CREATE INDEX IF NOT EXISTS idx_character_knowledge_domain_provenance
    ON aios.character_knowledge_domain (character_id, provenance, enabled);

COMMENT ON TABLE aios.character_knowledge_domain IS
'Character corpus-domain affinity/eligibility, not knowledge. Native/crossover rows may be projected from authoritative identity; acquired/granted rows come from separate study/operator policy.';
COMMENT ON COLUMN aios.character_knowledge_domain.relationship IS
'Semantic relationship to the information domain: native, crossover, acquired, or granted.';
COMMENT ON COLUMN aios.character_knowledge_domain.provenance IS
'Authority that created this affinity. identity_facet is one-way projection from accepted identity and is never inferred from corpus content.';
COMMENT ON COLUMN aios.character_knowledge_domain.source_facet_id IS
'Accepted identity facet backing identity-derived native/crossover affinity; null for study/operator relationships.';

COMMIT;
