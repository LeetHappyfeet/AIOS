"""Bounded scheduler telemetry; parked and delayed work never inflates runnable lag."""
from __future__ import annotations

STAGES_SQL = """
WITH pending AS (
 SELECT q.*, CASE
   WHEN q.status='running' THEN 'running'
   WHEN q.job_type IN ('goal_plan_continuation','goal_planning_inference')
        AND NOT COALESCE(a.eligible,false) THEN 'parked'
   WHEN q.run_after>now() THEN 'delayed'
   WHEN q.job_type='goal_plan_continuation' AND EXISTS (
        SELECT 1 FROM aios.pipeline_job r WHERE r.status='running'
          AND r.job_type='goal_plan_continuation'
          AND r.payload->>'instance_id'=q.payload->>'instance_id') THEN 'waiting'
   ELSE 'runnable' END AS queue_state
 FROM aios.pipeline_job q
 LEFT JOIN aios.instance_goal_scheduling a
   ON q.job_type IN ('goal_plan_continuation','goal_planning_inference')
  AND a.instance_id=(q.payload->>'instance_id')::uuid
 WHERE q.status IN ('queued','running')
)
SELECT job_type,resource_class,scheduling_lane,
 count(*) FILTER (WHERE queue_state='runnable')::integer AS queued,
 count(*) FILTER (WHERE queue_state='running')::integer AS running,
 count(*) FILTER (WHERE queue_state='parked')::integer AS parked,
 count(*) FILTER (WHERE queue_state='delayed')::integer AS delayed,
 count(*) FILTER (WHERE queue_state='waiting')::integer AS waiting,
 COALESCE(extract(epoch FROM now()-min(created_at) FILTER (WHERE queue_state='runnable')),0)::double precision AS oldest_seconds
FROM pending GROUP BY job_type,resource_class,scheduling_lane
ORDER BY oldest_seconds DESC,queued DESC
"""

RATES_SQL = """
SELECT
 (SELECT count(*) FROM aios.pipeline_job WHERE created_at>=now()-interval '30 seconds')::integer AS arrivals_30s,
 (SELECT count(*) FROM aios.pipeline_job WHERE status='done' AND updated_at>=now()-interval '30 seconds')::integer AS done_30s,
 (SELECT count(*) FROM aios.pipeline_job WHERE scheduling_lane='LIVE' AND created_at>=now()-interval '30 seconds')::integer AS live_arrivals_30s,
 (SELECT count(*) FROM aios.pipeline_job WHERE scheduling_lane='LIVE' AND status='done' AND updated_at>=now()-interval '30 seconds')::integer AS live_done_30s,
 (SELECT count(*) FROM aios.pipeline_job WHERE status='failed' AND updated_at>=now()-interval '60 seconds')::integer AS failed_60s
"""


def lane_totals(stages):
    lanes = {}
    for row in stages:
        key = (row['resource_class'], row['scheduling_lane'])
        lane = lanes.setdefault(key, dict(resource_class=key[0], scheduling_lane=key[1],
            queued=0, running=0, parked=0, delayed=0, waiting=0, oldest_seconds=0.0))
        for field in ('queued', 'running', 'parked', 'delayed', 'waiting'):
            lane[field] += int(row[field] or 0)
        lane['oldest_seconds'] = max(lane['oldest_seconds'], float(row['oldest_seconds'] or 0))
    return list(lanes.values())
