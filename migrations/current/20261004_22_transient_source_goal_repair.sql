-- Repair two demonstrated V11 scene-only goals without touching unrelated intentions.
-- Source receipt and source-node identity are required; no global text-only deletion.
BEGIN;
UPDATE aios.character_agent_goal g
SET status='cancelled',
    meta=g.meta || jsonb_build_object(
        'resolution_kind','transient_scene_action_repair',
        'resolution_source','source-goal-admission-v2',
        'resolved_at',now()::text)
FROM aios.character_goal_admission_receipt r
WHERE r.instance_id=g.instance_id
  AND r.source_node_id=g.source_node_id
  AND r.source_node_id IS NOT NULL
  AND r.decision='eligible'
  AND lower(trim(r.objective)) IN ('be over here','get a glass of water')
  AND lower(trim(g.goal_text)) IN
      ('renamon intends to be over here.','renamon intends to get a glass of water.')
  AND g.status IN ('active','scheduled','dormant');
COMMIT;
