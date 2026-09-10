-- AIOS domain migration: pipeline scheduler and active-job identity
-- Consolidates node/section/claim/entity job identity with bounded parallel
-- execution leases, resource classes, partitioning, and scheduling lanes.

BEGIN;

-- AIOS pipeline job identity fix
-- 2026-09-03
--
-- The original partial unique index only keyed jobs by payload.node_id. Jobs
-- scoped by section_id (claim extraction and RDF promotion) therefore all
-- collapsed onto NULL and were not protected from duplicate scheduling.


DROP INDEX IF EXISTS aios.uniq_pipeline_job_identity;

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_node_active
    ON aios.pipeline_job (
        job_type,
        (payload->>'node_id')
    )
    WHERE status IN ('queued', 'running')
      AND payload ? 'node_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_section_active
    ON aios.pipeline_job (
        job_type,
        (payload->>'section_id')
    )
    WHERE status IN ('queued', 'running')
      AND payload ? 'section_id';

-- Singleton/global stages (currently rdf_liminal_classify) have neither a
-- node_id nor section_id. Protect one active job per type.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_global_active
    ON aios.pipeline_job (job_type)
    WHERE status IN ('queued', 'running')
      AND NOT (payload ? 'node_id')
      AND NOT (payload ? 'section_id');

-- -------------------------------------------------

-- AIOS pipeline claim-scoped job identity
-- 2026-09-05
--
-- Claim-scoped jobs (normalize_proposition, rdf_epistemic_project, etc.) must
-- be unique per claim, not globally unique per job_type.


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

-- -------------------------------------------------

-- AIOS semantic topology pipeline job identity fix
-- 2026-09-06
--
-- assertion-scoped and acquisition-scoped topology jobs are independent work
-- items. They must not fall through to the global singleton job identity.


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

-- -------------------------------------------------

-- AIOS bounded parallel execution scheduler
-- 2026-09-08
--
-- Adds explicit worker leases/resource classes and fixes active-job identity
-- for entity-scoped jobs that previously fell through to the global singleton
-- index.


ALTER TABLE aios.pipeline_job
    ADD COLUMN IF NOT EXISTS resource_class text,
    ADD COLUMN IF NOT EXISTS partition_key text,
    ADD COLUMN IF NOT EXISTS worker_id text,
    ADD COLUMN IF NOT EXISTS claimed_at timestamptz,
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

UPDATE aios.pipeline_job
SET resource_class = CASE job_type
    WHEN 'discover_characters' THEN 'FAST_SQL'
    WHEN 'dag_to_document_section' THEN 'FAST_SQL'
    WHEN 'extract_claims' THEN 'NLP'
    WHEN 'resolve_claim_context' THEN 'SEMANTIC'
    WHEN 'normalize_proposition' THEN 'SEMANTIC'
    WHEN 'project_character_knowledge' THEN 'SEMANTIC'
    WHEN 'derive_claim_topology' THEN 'SEMANTIC'
    WHEN 'derive_character_acquisition_topology' THEN 'SEMANTIC'
    WHEN 'derive_world_assertion_topology' THEN 'SEMANTIC'
    WHEN 'resolve_generated_facts' THEN 'RECONCILIATION'
    WHEN 'rdf_epistemic_project' THEN 'RDF'
    WHEN 'rdf_liminal_promote' THEN 'RDF'
    WHEN 'rdf_liminal_classify' THEN 'RDF'
    WHEN 'project_world_topology' THEN 'RDF'
    WHEN 'assign_narratives' THEN 'GLOBAL'
    ELSE 'GLOBAL'
END
WHERE resource_class IS NULL;

ALTER TABLE aios.pipeline_job
    ALTER COLUMN resource_class SET DEFAULT 'GLOBAL',
    ALTER COLUMN resource_class SET NOT NULL;

ALTER TABLE aios.pipeline_job
    DROP CONSTRAINT IF EXISTS ck_pipeline_job_resource_class;

ALTER TABLE aios.pipeline_job
    ADD CONSTRAINT ck_pipeline_job_resource_class
    CHECK (resource_class IN (
        'FAST_SQL','NLP','SEMANTIC','VECTOR','RDF','RECONCILIATION','GLOBAL'
    ));

DROP INDEX IF EXISTS aios.ux_pipeline_job_global_active;

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_character_active
    ON aios.pipeline_job (job_type, (payload->>'character_id'))
    WHERE status IN ('queued','running') AND payload ? 'character_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_world_active
    ON aios.pipeline_job (job_type, (payload->>'world_id'))
    WHERE status IN ('queued','running') AND payload ? 'world_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_assertion_active
    ON aios.pipeline_job (job_type, (payload->>'assertion_id'))
    WHERE status IN ('queued','running') AND payload ? 'assertion_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_acquisition_active
    ON aios.pipeline_job (job_type, (payload->>'acquisition_id'))
    WHERE status IN ('queued','running') AND payload ? 'acquisition_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_job_global_active
    ON aios.pipeline_job (job_type)
    WHERE status IN ('queued','running')
      AND NOT (payload ? 'node_id')
      AND NOT (payload ? 'section_id')
      AND NOT (payload ? 'claim_id')
      AND NOT (payload ? 'character_id')
      AND NOT (payload ? 'world_id')
      AND NOT (payload ? 'assertion_id')
      AND NOT (payload ? 'acquisition_id');

CREATE INDEX IF NOT EXISTS ix_pipeline_job_scheduler_ready
    ON aios.pipeline_job (resource_class, priority, created_at)
    WHERE status='queued';

CREATE INDEX IF NOT EXISTS ix_pipeline_job_lease_expiry
    ON aios.pipeline_job (lease_expires_at)
    WHERE status='running';

-- -------------------------------------------------

DROP INDEX IF EXISTS aios.ux_pipeline_job_node_active;
DROP INDEX IF EXISTS aios.ux_pipeline_job_section_active;
DROP INDEX IF EXISTS aios.ux_pipeline_job_claim_active;
DROP INDEX IF EXISTS aios.ux_pipeline_job_character_active;
DROP INDEX IF EXISTS aios.ux_pipeline_job_world_active;
DROP INDEX IF EXISTS aios.ux_pipeline_job_assertion_active;
DROP INDEX IF EXISTS aios.ux_pipeline_job_acquisition_active;
DROP INDEX IF EXISTS aios.ux_pipeline_job_global_active;

CREATE UNIQUE INDEX ux_pipeline_job_node_active
    ON aios.pipeline_job (job_type, (payload->>'node_id'))
    WHERE status IN ('queued','running')
      AND payload ? 'node_id';

CREATE UNIQUE INDEX ux_pipeline_job_section_active
    ON aios.pipeline_job (job_type, (payload->>'section_id'))
    WHERE status IN ('queued','running')
      AND payload ? 'section_id';

CREATE UNIQUE INDEX ux_pipeline_job_claim_active
    ON aios.pipeline_job (job_type, (payload->>'claim_id'))
    WHERE status IN ('queued','running')
      AND payload ? 'claim_id';

CREATE UNIQUE INDEX ux_pipeline_job_character_active
    ON aios.pipeline_job (job_type, (payload->>'character_id'))
    WHERE status IN ('queued','running')
      AND payload ? 'character_id';

CREATE UNIQUE INDEX ux_pipeline_job_world_active
    ON aios.pipeline_job (job_type, (payload->>'world_id'))
    WHERE status IN ('queued','running')
      AND payload ? 'world_id';

CREATE UNIQUE INDEX ux_pipeline_job_assertion_active
    ON aios.pipeline_job (job_type, (payload->>'assertion_id'))
    WHERE status IN ('queued','running')
      AND payload ? 'assertion_id';

CREATE UNIQUE INDEX ux_pipeline_job_acquisition_active
    ON aios.pipeline_job (job_type, (payload->>'acquisition_id'))
    WHERE status IN ('queued','running')
      AND payload ? 'acquisition_id';

CREATE UNIQUE INDEX ux_pipeline_job_global_active
    ON aios.pipeline_job (job_type)
    WHERE status IN ('queued','running')
      AND NOT (payload ? 'node_id')
      AND NOT (payload ? 'section_id')
      AND NOT (payload ? 'claim_id')
      AND NOT (payload ? 'character_id')
      AND NOT (payload ? 'world_id')
      AND NOT (payload ? 'assertion_id')
      AND NOT (payload ? 'acquisition_id');

-- -------------------------------------------------

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
