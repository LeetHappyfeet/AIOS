-- Corpus scope access classes: public reference, character-domain, or explicit restricted.
BEGIN;

-- Fresh installs reach this migration before corpus_research_phase1.sql, which
-- historically created corpus_scope. Establish the base scope relation here so
-- access-class evolution does not depend on a later lexical migration.
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

ALTER TABLE aios.corpus_scope
    ADD COLUMN IF NOT EXISTS access_class text NOT NULL DEFAULT 'restricted'
        CHECK (access_class IN ('public','domain','restricted'));

UPDATE aios.corpus_scope
SET access_class='restricted'
WHERE scope_key='unclassified';

COMMENT ON COLUMN aios.corpus_scope.access_class IS
'Search eligibility: public is available to all characters (subject to explicit deny); domain is granted from character knowledge-domain eligibility; restricted requires explicit ACL.';

COMMIT;
