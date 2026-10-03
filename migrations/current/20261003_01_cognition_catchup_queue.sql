-- Keep catch-up ownership per perceiving character instance, not globally.
-- Existing global uniqueness treats payloads with only instance_id as one key.
BEGIN;

DROP INDEX IF EXISTS aios.ux_pipeline_job_global_active;
CREATE UNIQUE INDEX ux_pipeline_job_global_active
ON aios.pipeline_job (job_type)
WHERE status IN ('queued','running')
  AND job_type <> 'message_cognition_catchup'
  AND NOT (payload ? 'node_id')
  AND NOT (payload ? 'section_id')
  AND NOT (payload ? 'claim_id')
  AND NOT (payload ? 'character_id')
  AND NOT (payload ? 'world_id')
  AND NOT (payload ? 'assertion_id')
  AND NOT (payload ? 'acquisition_id');

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_cognition_catchup_active
ON aios.pipeline_job (job_type, (payload->>'instance_id'))
WHERE status IN ('queued','running')
  AND job_type='message_cognition_catchup';

COMMIT;
