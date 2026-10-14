-- Cognitive jobs have operation/transaction/task owners, not one global owner.
-- Preserve true global singletons and the existing source/scope indexes.
DROP INDEX IF EXISTS aios.ux_pipeline_job_global_active;
CREATE UNIQUE INDEX ux_pipeline_job_global_active
ON aios.pipeline_job (job_type)
WHERE status IN ('queued','running')
  AND NOT (payload ? 'node_id')
  AND NOT (payload ? 'section_id')
  AND NOT (payload ? 'claim_id')
  AND NOT (payload ? 'character_id')
  AND NOT (payload ? 'world_id')
  AND NOT (payload ? 'assertion_id')
  AND NOT (payload ? 'acquisition_id')
  AND NOT (payload ? 'scope_key')
  AND NOT (payload ? 'instance_id')
  AND NOT (payload ? 'operation_id')
  AND NOT (payload ? 'transaction_id')
  AND NOT (payload ? 'task_id');

CREATE UNIQUE INDEX ux_pipeline_job_operation_active
ON aios.pipeline_job(job_type,(payload->>'operation_id'))
WHERE status IN ('queued','running') AND payload ? 'operation_id';
CREATE UNIQUE INDEX ux_pipeline_job_transaction_active
ON aios.pipeline_job(job_type,(payload->>'transaction_id'))
WHERE status IN ('queued','running') AND payload ? 'transaction_id'
  AND NOT (payload ? 'operation_id');
CREATE UNIQUE INDEX ux_pipeline_job_task_active
ON aios.pipeline_job(job_type,(payload->>'task_id'))
WHERE status IN ('queued','running') AND payload ? 'task_id'
  AND NOT (payload ? 'operation_id') AND NOT (payload ? 'transaction_id');
CREATE UNIQUE INDEX ux_pipeline_job_instance_active
ON aios.pipeline_job(job_type,(payload->>'instance_id'))
WHERE status IN ('queued','running') AND payload ? 'instance_id'
  AND NOT (payload ? 'operation_id') AND NOT (payload ? 'transaction_id')
  AND NOT (payload ? 'task_id') AND NOT (payload ? 'node_id')
  AND NOT (payload ? 'section_id') AND NOT (payload ? 'claim_id')
  AND NOT (payload ? 'character_id') AND NOT (payload ? 'world_id')
  AND NOT (payload ? 'assertion_id') AND NOT (payload ? 'acquisition_id')
  AND NOT (payload ? 'scope_key');

-- Repair only planning admissions known to have failed at the second enqueue.
-- Initial admission failures rolled back and need no repair. Keep other model,
-- validation, and stale-context failures terminal. Preserve the original error.
WITH repair AS (
 SELECT DISTINCT ON (p.instance_id) p.plan_id,o.operation_id
 FROM aios.character_goal_plan p
 JOIN aios.character_cognitive_operation o ON o.operation_id=p.operation_id
 JOIN aios.character_agent_goal g ON g.goal_id=p.goal_id AND g.instance_id=p.instance_id
 JOIN aios.character_goal_attempt a ON a.attempt_id=p.attempt_id AND a.closed_at IS NULL
 WHERE p.status='failed' AND o.status='failed' AND o.operation_type='planning.propose'
   AND o.result->>'error'='could not enqueue goal planning' AND g.status='active'
   AND NOT EXISTS (SELECT 1 FROM aios.character_goal_plan q
                   WHERE q.instance_id=p.instance_id AND q.status='queued')
 ORDER BY p.instance_id,p.revision DESC,p.created_at DESC
), requeued AS (
 UPDATE aios.character_cognitive_operation o
 SET status='queued',input=o.input || jsonb_build_object('enqueue_repair',o.result),
     result='{}'::jsonb,started_at=NULL,completed_at=NULL,reconciled_at=NULL,updated_at=now()
 FROM repair r WHERE o.operation_id=r.operation_id RETURNING r.plan_id
)
UPDATE aios.character_goal_plan p SET status='queued',updated_at=now()
FROM requeued r WHERE p.plan_id=r.plan_id;
-- Existing cognition recovery recreates a missing cognitive_operation job.
