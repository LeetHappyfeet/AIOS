-- Corpus scope access classes: public reference, character-domain, or explicit restricted.
BEGIN;

ALTER TABLE aios.corpus_scope
    ADD COLUMN IF NOT EXISTS access_class text NOT NULL DEFAULT 'restricted'
        CHECK (access_class IN ('public','domain','restricted'));

UPDATE aios.corpus_scope
SET access_class='restricted'
WHERE scope_key='unclassified';

COMMENT ON COLUMN aios.corpus_scope.access_class IS
'Search eligibility: public is available to all characters (subject to explicit deny); domain is granted from character knowledge-domain eligibility; restricted requires explicit ACL.';

COMMIT;
