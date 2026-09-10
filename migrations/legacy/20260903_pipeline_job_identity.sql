-- AIOS pipeline job identity fix
-- 2026-09-03
--
-- The original partial unique index only keyed jobs by payload.node_id. Jobs
-- scoped by section_id (claim extraction and RDF promotion) therefore all
-- collapsed onto NULL and were not protected from duplicate scheduling.

BEGIN;

DROP INDEX IF EXISTS aios.uniq_pipeline_job_identity;

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_node_active
    ON aios.pipeline_job (
        job_type,
        (payload->>'node_id')
    )
    WHERE status IN ('queued', 'running')
      AND payload ? 'node_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_section_active
    ON aios.pipeline_job (
        job_type,
        (payload->>'section_id')
    )
    WHERE status IN ('queued', 'running')
      AND payload ? 'section_id';

-- Singleton/global stages (currently rdf_liminal_classify) have neither a
-- node_id nor section_id. Protect one active job per type.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_global_active
    ON aios.pipeline_job (job_type)
    WHERE status IN ('queued', 'running')
      AND NOT (payload ? 'node_id')
      AND NOT (payload ? 'section_id');

COMMIT;
