-- AIOS semantic topology pipeline job identity fix
-- 2026-09-06
--
-- assertion-scoped and acquisition-scoped topology jobs are independent work
-- items. They must not fall through to the global singleton job identity.

BEGIN;

DROP INDEX IF EXISTS aios.ux_pipeline_job_global_active;

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_assertion_active
    ON aios.pipeline_job (
        job_type,
        (payload->>'assertion_id')
    )
    WHERE status IN ('queued', 'running')
      AND payload ? 'assertion_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_acquisition_active
    ON aios.pipeline_job (
        job_type,
        (payload->>'acquisition_id')
    )
    WHERE status IN ('queued', 'running')
      AND payload ? 'acquisition_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_global_active
    ON aios.pipeline_job (job_type)
    WHERE status IN ('queued', 'running')
      AND NOT (payload ? 'node_id')
      AND NOT (payload ? 'section_id')
      AND NOT (payload ? 'claim_id')
      AND NOT (payload ? 'assertion_id')
      AND NOT (payload ? 'acquisition_id');

COMMIT;
