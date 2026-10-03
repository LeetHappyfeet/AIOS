# Deferred historical cognition: independent recovery sweep

The pipeline runner starts `_deferred_cognition_recovery_loop` at startup and
cancels it on shutdown. The default interval is 60 seconds. Its work is limited
to discovering and enqueueing ordinary `message_cognition_catchup` jobs; it
does not perform inference or modify cognition/goal receipts itself.

## Discovery and safety

- Only historical commits with both `enrichment_pending=true` and
  `enrichment_deferred=true` are candidates.
- Candidate nodes must be ancestors of the character instance's current
  source head on its current timeline. This excludes other timelines and stale
  history.
- Active `message_cognition` leases, including older requests lacking
  `task_id`, block redispatch. An unleased request less than two minutes old
  is protected during its initial lease setup.
- Queued/running cognition-enrichment and catch-up pipeline jobs also block
  rediscovery.
- Failed/invalid inference attempts are backed off exponentially according
  to their count within the last hour, bounded by 15 minutes and measured from
  the most recent attempt's completion.
- Existing `enqueue_job` provides an atomic per-instance dedupe for queued
  catch-up jobs to close the race with the normal handler continuation.
  A currently running handler is explicitly allowed to queue its next job.
- At most 16 candidate character instances are processed per sweep. PostgreSQL
  lease reaping updates only expired requests; valid long-running generations
  are never abandoned based solely on a quiet `last_progress_at`.

## Verification

After deployment/restarting `runner_v2`, inspect:

```sql
SELECT job_id,status,run_after,payload
FROM aios.pipeline_job
WHERE job_type='message_cognition_catchup'
ORDER BY created_at DESC LIMIT 10;

SELECT request_id,task_id,status,created_at,completed_at,error
FROM aios.inference_request
WHERE worker_class='message_cognition'
ORDER BY created_at DESC LIMIT 10;
```

A failed orphan should be reaped, followed by an independently enqueued
catch-up when its cooldown expires. A new inference request should have
`task_id` set to the reviewed source node. Completed historical enrichment
receipts are never retried. Subsequent managed-goal reconciliation remains
subject to the original chronology checks.

The fast regression suite verifies bounded ancestry discovery, exclusion
criteria, failure cooldown, atomic enqueue behavior and runner task lifecycle.
It does not substitute for a live PostgreSQL/restart integration test.
