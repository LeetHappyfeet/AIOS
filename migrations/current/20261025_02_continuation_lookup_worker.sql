-- Slow discovery is leased background work, never a supervisor transaction.
ALTER TABLE aios.character_goal_plan
 ADD COLUMN continuation_token uuid,
 ADD COLUMN continuation_lease_until timestamptz,
 ADD COLUMN continuation_completed_request_id uuid;
CREATE UNIQUE INDEX uq_goal_plan_continuation_job
 ON aios.pipeline_job((payload->>'plan_id'))
 WHERE job_type='goal_plan_continuation' AND status IN ('queued','running');
ALTER TABLE aios.pipeline_job DROP CONSTRAINT IF EXISTS ck_pipeline_job_resource_class;
ALTER TABLE aios.pipeline_job ADD CONSTRAINT ck_pipeline_job_resource_class
 CHECK(resource_class IN ('FAST_SQL','NLP','SEMANTIC','VECTOR','RDF','RECONCILIATION','GLOBAL','LOOKUP'));
