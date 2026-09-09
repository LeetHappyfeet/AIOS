-- Runtime source-perception cursors for HUD assembly
-- 2026-09-06
BEGIN;

ALTER TABLE aios.character_runtime_state
    ADD COLUMN IF NOT EXISTS source_timeline_id uuid REFERENCES aios.timeline(timeline_id),
    ADD COLUMN IF NOT EXISTS source_head_node_id uuid REFERENCES aios.dag_node(node_id);

CREATE INDEX IF NOT EXISTS idx_character_runtime_source_timeline
    ON aios.character_runtime_state (source_timeline_id);

CREATE INDEX IF NOT EXISTS idx_character_runtime_source_head
    ON aios.character_runtime_state (source_head_node_id);

-- Backfill existing normal session runtimes from their authorized source
-- timeline.  The world anchor remains the immutable branch origin; the runtime
-- source head is the moving perception boundary and therefore starts at the
-- latest source node currently available. Forks are left conservative because
-- their perception boundary must be copied explicitly from their parent.
UPDATE aios.character_runtime_state rs
SET source_timeline_id = COALESCE(rs.source_timeline_id, w.anchor_timeline_id),
    source_head_node_id = COALESCE(
        rs.source_head_node_id,
        latest_source.node_id,
        w.anchor_node_id
    ),
    updated_at = now()
FROM aios.world w
JOIN aios.character_instance ci
  ON ci.current_world_id=w.world_id
LEFT JOIN LATERAL (
    SELECT dn.node_id
    FROM aios.dag_node dn
    WHERE dn.timeline_id=w.anchor_timeline_id
    ORDER BY dn.event_id DESC
    LIMIT 1
) latest_source ON true
WHERE w.world_id = rs.world_id
  AND ci.instance_id=rs.instance_id
  AND ci.parent_instance_id IS NULL
  AND (
      (rs.source_timeline_id IS NULL AND w.anchor_timeline_id IS NOT NULL)
      OR (rs.source_head_node_id IS NULL AND w.anchor_node_id IS NOT NULL)
  );

COMMIT;
