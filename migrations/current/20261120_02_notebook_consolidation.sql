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

-- One queued/running summary decision per notebook. This does not create a
-- new inference path for ordinary chat or HUD rendering.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_notebook_consolidate_active
  ON aios.pipeline_job(job_type,(payload->>'notebook_id'))
  WHERE job_type='notebook_consolidate' AND status IN ('queued','running');

COMMIT;
