BEGIN;

-- Optional semantic index receipts. Raw note text remains solely in PostgreSQL.
CREATE TABLE IF NOT EXISTS aios.character_notebook_vector_state (
  entry_id uuid PRIMARY KEY REFERENCES aios.character_notebook_entry(entry_id) ON DELETE CASCADE,
  notebook_id uuid NOT NULL REFERENCES aios.character_notebook(notebook_id) ON DELETE CASCADE,
  indexed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_notebook_vectors_notebook
  ON aios.character_notebook_vector_state(notebook_id);

-- One active vector index job per notebook, independently of other projects.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_notebook_index_active
  ON aios.pipeline_job(job_type,(payload->>'notebook_id'))
  WHERE job_type='notebook_index' AND status IN ('queued','running');

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
  AND job_type NOT IN ('character_reminder_due','notebook_consolidate','notebook_index');
COMMIT;
