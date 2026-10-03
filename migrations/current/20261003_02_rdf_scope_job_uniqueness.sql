-- Allow independently coalesced semantic scope projection jobs.
-- The legacy generic singleton index previously captured payloads with scope_key
-- and silently rejected every additional scope via enqueue_job ON CONFLICT DO NOTHING.
DROP INDEX IF EXISTS aios.ux_pipeline_job_global_active;
CREATE UNIQUE INDEX ux_pipeline_job_global_active
ON aios.pipeline_job (job_type)
WHERE status IN ('queued', 'running')
  AND NOT (payload ? 'node_id')
  AND NOT (payload ? 'section_id')
  AND NOT (payload ? 'claim_id')
  AND NOT (payload ? 'character_id')
  AND NOT (payload ? 'world_id')
  AND NOT (payload ? 'assertion_id')
  AND NOT (payload ? 'acquisition_id')
  AND NOT (payload ? 'scope_key');
-- ux_pipeline_project_semantic_scope_active remains the per-scope guard.
