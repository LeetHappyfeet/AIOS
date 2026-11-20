BEGIN;

-- Immutable successive interpretations. Raw entries are never rewritten.
CREATE TABLE IF NOT EXISTS aios.character_notebook_summary (
  notebook_id uuid NOT NULL REFERENCES aios.character_notebook(notebook_id) ON DELETE CASCADE,
  revision integer NOT NULL CHECK(revision > 0),
  covered_count bigint NOT NULL CHECK(covered_count > 0),
  summary jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(notebook_id, revision),
  UNIQUE(notebook_id, covered_count),
  CHECK (jsonb_typeof(summary)='object')
);

-- Preserve the existing per-instance uniqueness for other jobs, but allow
-- different notebooks of the same instance to consolidate independently.
DROP INDEX IF EXISTS aios.ux_pipeline_job_instance_active;
CREATE UNIQUE INDEX ux_pipeline_job_instance_active
ON aios.pipeline_job(job_type,(payload->>'instance_id'))
WHERE status IN ('queued','running') AND payload ? 'instance_id'
  AND NOT (payload ? 'operation_id') AND NOT (payload ? 'transaction_id')
  AND NOT (payload ? 'task_id') AND NOT (payload ? 'node_id')
  AND NOT (payload ? 'section_id') AND NOT (payload ? 'claim_id')
  AND NOT (payload ? 'character_id') AND NOT (payload ? 'world_id')
  AND NOT (payload ? 'assertion_id') AND NOT (payload ? 'acquisition_id')
  AND NOT (payload ? 'scope_key')
  AND job_type NOT IN ('character_reminder_due','notebook_consolidate');

-- One queued/running summary decision per notebook. This does not create a
-- new inference path for ordinary chat or HUD rendering.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_notebook_consolidate_active
  ON aios.pipeline_job(job_type,(payload->>'notebook_id'))
  WHERE job_type='notebook_consolidate' AND status IN ('queued','running');

COMMIT;
