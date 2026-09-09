BEGIN;

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

COMMIT;
