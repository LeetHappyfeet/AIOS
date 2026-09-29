-- Resume foreground scheduling for an already active HUD source after upgrade.
-- Only the exact current source is adopted; historical character work stays
-- in the backlog quota. New turns extend this range through ingest_api.
BEGIN;

INSERT INTO aios.pipeline_foreground_lineage (
    instance_id, character_id, timeline_id, first_event_id,
    head_event_id, head_node_id, activated_at, expires_at, reason, updated_at
)
SELECT hr.instance_id, ci.character_id, dn.timeline_id, dn.event_id,
       dn.event_id, dn.node_id, now(), now()+interval '1 hour',
       'active_source_upgrade', now()
FROM aios.character_hud_readiness hr
JOIN aios.character_instance ci ON ci.instance_id=hr.instance_id
JOIN aios.dag_node dn ON dn.node_id=hr.source_head_node_id
WHERE hr.live
  AND hr.source_head_node_id IS NOT NULL
ON CONFLICT (instance_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_knowledge_acquisition_topology_pending
ON aios.knowledge_acquisition_event(created_at, acquisition_id)
WHERE proposition_id IS NOT NULL AND processed_at IS NOT NULL;

COMMIT;
