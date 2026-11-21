BEGIN;

-- A character can have several pending reminders. Each due trigger owns a job.
DROP INDEX aios.ux_pipeline_job_instance_active;
CREATE UNIQUE INDEX ux_pipeline_job_instance_active
ON aios.pipeline_job(job_type,(payload->>'instance_id'))
WHERE status IN ('queued','running') AND payload ? 'instance_id'
  AND NOT (payload ? 'operation_id') AND NOT (payload ? 'transaction_id')
  AND NOT (payload ? 'task_id') AND NOT (payload ? 'node_id')
  AND NOT (payload ? 'section_id') AND NOT (payload ? 'claim_id')
  AND NOT (payload ? 'character_id') AND NOT (payload ? 'world_id')
  AND NOT (payload ? 'assertion_id') AND NOT (payload ? 'acquisition_id')
  AND NOT (payload ? 'scope_key') AND job_type <> 'character_reminder_due';

CREATE UNIQUE INDEX ux_pipeline_job_reminder_active
ON aios.pipeline_job(job_type,(payload->>'trigger_id'))
WHERE status IN ('queued','running') AND job_type='character_reminder_due';

COMMIT;
