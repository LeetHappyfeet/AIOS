-- AIOS pipeline claim-scoped job identity
-- 2026-09-05
--
-- Claim-scoped jobs (normalize_proposition, rdf_epistemic_project, etc.) must
-- be unique per claim, not globally unique per job_type.

BEGIN;

DROP INDEX IF EXISTS aios.ux_pipeline_job_global_active;

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_claim_active
    ON aios.pipeline_job (
        job_type,
        (payload->>'claim_id')
    )
    WHERE status IN ('queued', 'running')
      AND payload ? 'claim_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_global_active
    ON aios.pipeline_job (job_type)
    WHERE status IN ('queued', 'running')
      AND NOT (payload ? 'node_id')
      AND NOT (payload ? 'section_id')
      AND NOT (payload ? 'claim_id');

COMMIT;
