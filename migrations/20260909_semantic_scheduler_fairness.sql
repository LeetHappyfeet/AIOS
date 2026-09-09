BEGIN;

ALTER TABLE aios.pipeline_job
    ADD COLUMN IF NOT EXISTS scheduling_lane text;

UPDATE aios.pipeline_job
SET scheduling_lane = CASE
    WHEN job_type IN (
        'resolve_claim_context',
        'normalize_proposition',
        'project_character_knowledge'
    ) THEN 'LIVE'
    WHEN job_type='derive_claim_topology'
         AND payload->>'semantic_backfill'='proposition_leaves_20260909'
        THEN 'BACKGROUND'
    WHEN job_type IN (
        'derive_claim_topology',
        'derive_character_acquisition_topology',
        'derive_world_assertion_topology'
    ) THEN 'STRUCTURAL'
    ELSE 'DEFAULT'
END
WHERE scheduling_lane IS NULL;

ALTER TABLE aios.pipeline_job
    ALTER COLUMN scheduling_lane SET DEFAULT 'DEFAULT',
    ALTER COLUMN scheduling_lane SET NOT NULL;

ALTER TABLE aios.pipeline_job
    DROP CONSTRAINT IF EXISTS ck_pipeline_job_scheduling_lane;

ALTER TABLE aios.pipeline_job
    ADD CONSTRAINT ck_pipeline_job_scheduling_lane
    CHECK (scheduling_lane IN ('LIVE','STRUCTURAL','BACKGROUND','DEFAULT'));

UPDATE aios.pipeline_job
SET priority=95,
    updated_at=now()
WHERE status='queued'
  AND job_type='derive_claim_topology'
  AND payload->>'semantic_backfill'='proposition_leaves_20260909';

CREATE INDEX IF NOT EXISTS ix_pipeline_job_scheduler_lane
    ON aios.pipeline_job (resource_class, scheduling_lane, priority, created_at)
    WHERE status='queued';

COMMIT;
