-- Runtime source-cursor invariants.
--
-- A runtime DAG is not a source DAG. Older activation code could bootstrap
-- source_timeline_id from the runtime timeline itself when no liminal source
-- existed yet. Represent absence of a source as NULL instead, and clear the
-- corresponding self-anchor so the first exact liminal ingress can bind it.

UPDATE aios.character_runtime_state
SET source_timeline_id = NULL,
    source_head_node_id = NULL,
    updated_at = now()
WHERE source_timeline_id = timeline_id;

UPDATE aios.world w
SET anchor_timeline_id = NULL,
    anchor_node_id = NULL
FROM aios.timeline t
WHERE w.world_type = 'runtime'
  AND w.anchor_timeline_id = t.timeline_id
  AND t.world_id = w.world_id;

-- A head coordinate is only meaningful when the node belongs to the declared
-- source timeline. Preserve the timeline binding but clear any inconsistent
-- head so later exact ingress can advance it safely.
UPDATE aios.character_runtime_state rs
SET source_head_node_id = NULL,
    updated_at = now()
WHERE rs.source_head_node_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM aios.dag_node dn
      WHERE dn.node_id = rs.source_head_node_id
        AND dn.timeline_id = rs.source_timeline_id
  );
