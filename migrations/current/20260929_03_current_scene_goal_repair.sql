BEGIN;

-- Repair only the current projection. Historical snapshots may correctly
-- record a goal that was active at the time of an earlier source node.
WITH cleared AS (
  UPDATE aios.character_scene_snapshot s
  SET scene_state=jsonb_set(s.scene_state,'{immediate_goal}','null'::jsonb,true),
      updated_at=now()
  FROM aios.character_hud_readiness r, aios.character_agent_goal g
  WHERE r.instance_id=s.instance_id
    AND s.source_head_node_id IS NOT DISTINCT FROM r.source_head_node_id
    AND s.source_timeline_id IS NOT DISTINCT FROM r.source_timeline_id
    AND g.instance_id=s.instance_id
    AND g.goal_id::text=(s.scene_state->'immediate_goal'->>'goal_id')
    AND g.status<>'active'
  RETURNING s.instance_id
), bumped AS (
  UPDATE aios.character_runtime_state
  SET state_version=state_version+1,updated_at=now()
  WHERE instance_id IN (SELECT instance_id FROM cleared)
  RETURNING instance_id
)
UPDATE aios.character_hud_readiness
SET status='dirty',dirty_since=now(),updated_at=now()
WHERE instance_id IN (SELECT instance_id FROM cleared);

COMMIT;
