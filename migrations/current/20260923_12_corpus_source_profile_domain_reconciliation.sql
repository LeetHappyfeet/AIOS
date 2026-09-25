-- Reconcile legacy corpus source-profile domain projection.
-- Source profiles are trusted deterministic routing; existing profile-assigned
-- documents should carry the same document-domain projection as new ingests.
BEGIN;

UPDATE aios.corpus_source_profile
SET knowledge_domain='fiction.digimon',
    updated_at=now(),
    meta=meta || '{"domain_reconciled":"20260923_12"}'::jsonb
WHERE profile_key='digimon-fandom'
  AND (knowledge_domain IS NULL OR knowledge_domain='');

INSERT INTO aios.corpus_document_domain (
    document_id, knowledge_domain, source, meta
)
SELECT
    cdc.document_id,
    csp.knowledge_domain,
    'source_profile',
    jsonb_build_object(
        'profile_key', csp.profile_key,
        'scope_key', csp.scope_key,
        'evidence', 'trusted_source_profile',
        'reconciled_by', '20260923_12'
    )
FROM aios.corpus_document_collection cdc
JOIN aios.corpus_source_profile csp
  ON csp.profile_id=cdc.profile_id
WHERE csp.enabled
  AND csp.knowledge_domain IS NOT NULL
  AND csp.knowledge_domain <> ''
ON CONFLICT (document_id, knowledge_domain) DO UPDATE
SET source='source_profile',
    meta=aios.corpus_document_domain.meta || EXCLUDED.meta;

COMMIT;
