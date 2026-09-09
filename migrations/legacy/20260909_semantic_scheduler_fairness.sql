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

UPDATE aios.pipeline_job
SET partition_key='claim:' || (payload->>'claim_id')
WHERE partition_key IS NULL
  AND job_type IN ('resolve_claim_context','normalize_proposition')
  AND payload ? 'claim_id';

UPDATE aios.pipeline_job pj
SET partition_key = CASE
    WHEN ccr.epistemic_scope='character'
         AND ccr.origin_character_id IS NOT NULL
         AND ccr.character_instance_id IS NOT NULL
        THEN 'char:' || ccr.origin_character_id
    WHEN ccr.source_id IS NOT NULL
        THEN 'source:' || ccr.source_id
    WHEN ccr.world_id IS NOT NULL
        THEN 'world:' || ccr.world_id::text || ':observed'
    ELSE 'claim:' || ccr.claim_id::text
END
FROM aios.claim_context_resolution ccr
WHERE pj.partition_key IS NULL
  AND pj.job_type IN ('derive_claim_topology','rdf_epistemic_project')
  AND pj.payload->>'claim_id'=ccr.claim_id::text;

UPDATE aios.pipeline_job pj
SET partition_key='char:' || ci.character_id
FROM aios.knowledge_acquisition_event kae
JOIN aios.character_instance ci ON ci.instance_id=kae.instance_id
WHERE pj.partition_key IS NULL
  AND pj.job_type='derive_character_acquisition_topology'
  AND pj.payload->>'acquisition_id'=kae.acquisition_id::text;

UPDATE aios.pipeline_job pj
SET partition_key='world:' || a.world_id::text || ':asserted'
FROM aios.world_proposition_assertion a
WHERE pj.partition_key IS NULL
  AND pj.job_type='derive_world_assertion_topology'
  AND pj.payload->>'assertion_id'=a.assertion_id::text;

UPDATE aios.pipeline_job
SET partition_key='world_id:' || (payload->>'world_id')
WHERE partition_key IS NULL AND payload ? 'world_id';

UPDATE aios.pipeline_job
SET partition_key='section_id:' || (payload->>'section_id')
WHERE partition_key IS NULL AND payload ? 'section_id';

UPDATE aios.pipeline_job
SET partition_key='node_id:' || (payload->>'node_id')
WHERE partition_key IS NULL AND payload ? 'node_id';

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

UPDATE aios.pipeline_job
SET status='queued',
    run_after=now(),
    worker_id=NULL,
    claimed_at=NULL,
    heartbeat_at=NULL,
    lease_expires_at=NULL,
    updated_at=now(),
    last_error=CASE
        WHEN COALESCE(last_error,'')='' THEN
            '[requeued legacy pre-lease running job]'
        ELSE
            last_error || ' [requeued legacy pre-lease running job]'
    END
WHERE status='running'
  AND worker_id IS NULL
  AND claimed_at IS NULL
  AND heartbeat_at IS NULL
  AND lease_expires_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_pipeline_job_scheduler_lane
    ON aios.pipeline_job (resource_class, scheduling_lane, priority, created_at)
    WHERE status='queued';

CREATE INDEX IF NOT EXISTS ix_pipeline_job_running_partition
    ON aios.pipeline_job (partition_key)
    WHERE status='running' AND partition_key IS NOT NULL;

COMMIT;
