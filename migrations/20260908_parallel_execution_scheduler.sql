-- AIOS bounded parallel execution scheduler
-- 2026-09-08
--
-- Adds explicit worker leases/resource classes and fixes active-job identity
-- for entity-scoped jobs that previously fell through to the global singleton
-- index.

BEGIN;

ALTER TABLE aios.pipeline_job
    ADD COLUMN IF NOT EXISTS resource_class text,
    ADD COLUMN IF NOT EXISTS partition_key text,
    ADD COLUMN IF NOT EXISTS worker_id text,
    ADD COLUMN IF NOT EXISTS claimed_at timestamptz,
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

UPDATE aios.pipeline_job
SET resource_class = CASE job_type
    WHEN 'discover_characters' THEN 'FAST_SQL'
    WHEN 'dag_to_document_section' THEN 'FAST_SQL'
    WHEN 'extract_claims' THEN 'NLP'
    WHEN 'resolve_claim_context' THEN 'SEMANTIC'
    WHEN 'normalize_proposition' THEN 'SEMANTIC'
    WHEN 'project_character_knowledge' THEN 'SEMANTIC'
    WHEN 'derive_claim_topology' THEN 'SEMANTIC'
    WHEN 'derive_character_acquisition_topology' THEN 'SEMANTIC'
    WHEN 'derive_world_assertion_topology' THEN 'SEMANTIC'
    WHEN 'resolve_generated_facts' THEN 'RECONCILIATION'
    WHEN 'rdf_epistemic_project' THEN 'RDF'
    WHEN 'rdf_liminal_promote' THEN 'RDF'
    WHEN 'rdf_liminal_classify' THEN 'RDF'
    WHEN 'project_world_topology' THEN 'RDF'
    WHEN 'assign_narratives' THEN 'GLOBAL'
    ELSE 'GLOBAL'
END
WHERE resource_class IS NULL;

ALTER TABLE aios.pipeline_job
    ALTER COLUMN resource_class SET DEFAULT 'GLOBAL',
    ALTER COLUMN resource_class SET NOT NULL;

ALTER TABLE aios.pipeline_job
    DROP CONSTRAINT IF EXISTS ck_pipeline_job_resource_class;

ALTER TABLE aios.pipeline_job
    ADD CONSTRAINT ck_pipeline_job_resource_class
    CHECK (resource_class IN (
        'FAST_SQL','NLP','SEMANTIC','VECTOR','RDF','RECONCILIATION','GLOBAL'
    ));

DROP INDEX IF EXISTS aios.ux_pipeline_job_global_active;

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_character_active
    ON aios.pipeline_job (job_type, (payload->>'character_id'))
    WHERE status IN ('queued','running') AND payload ? 'character_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_world_active
    ON aios.pipeline_job (job_type, (payload->>'world_id'))
    WHERE status IN ('queued','running') AND payload ? 'world_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_assertion_active
    ON aios.pipeline_job (job_type, (payload->>'assertion_id'))
    WHERE status IN ('queued','running') AND payload ? 'assertion_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_acquisition_active
    ON aios.pipeline_job (job_type, (payload->>'acquisition_id'))
    WHERE status IN ('queued','running') AND payload ? 'acquisition_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_global_active
    ON aios.pipeline_job (job_type)
    WHERE status IN ('queued','running')
      AND NOT (payload ? 'node_id')
      AND NOT (payload ? 'section_id')
      AND NOT (payload ? 'claim_id')
      AND NOT (payload ? 'character_id')
      AND NOT (payload ? 'world_id')
      AND NOT (payload ? 'assertion_id')
      AND NOT (payload ? 'acquisition_id');

CREATE INDEX IF NOT EXISTS ix_pipeline_job_scheduler_ready
    ON aios.pipeline_job (resource_class, priority, created_at)
    WHERE status='queued';

CREATE INDEX IF NOT EXISTS ix_pipeline_job_lease_expiry
    ON aios.pipeline_job (lease_expires_at)
    WHERE status='running';

COMMIT;
