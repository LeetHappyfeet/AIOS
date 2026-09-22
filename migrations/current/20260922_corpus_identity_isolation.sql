-- Corpus canon/reference identity isolation.
BEGIN;

ALTER TABLE aios.corpus_document
    ADD COLUMN IF NOT EXISTS epistemic_namespace text NOT NULL DEFAULT 'reference',
    ADD COLUMN IF NOT EXISTS identity_binding text NOT NULL DEFAULT 'external'
        CHECK (identity_binding IN ('external','world','character'));

UPDATE aios.corpus_document
SET epistemic_namespace='reference',
    identity_binding='external'
WHERE epistemic_namespace IS NULL OR identity_binding IS NULL;

COMMENT ON COLUMN aios.corpus_document.epistemic_namespace IS
'Namespace for names mentioned by the document (for example canon:digimon). It is provenance, not active character identity.';
COMMENT ON COLUMN aios.corpus_document.identity_binding IS
'Controls whether named entities in corpus material may bind to runtime identities. external is the safe default and forbids autobiographical binding.';

COMMIT;
