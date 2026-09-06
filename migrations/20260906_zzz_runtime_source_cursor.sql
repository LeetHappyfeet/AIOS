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

-- Backfill existing runtime instances from their immutable world branch anchor.
UPDATE aios.character_runtime_state rs
SET source_timeline_id = COALESCE(rs.source_timeline_id, w.anchor_timeline_id),
    source_head_node_id = COALESCE(rs.source_head_node_id, w.anchor_node_id),
    updated_at = now()
FROM aios.world w
WHERE w.world_id = rs.world_id
  AND (
      (rs.source_timeline_id IS NULL AND w.anchor_timeline_id IS NOT NULL)
      OR (rs.source_head_node_id IS NULL AND w.anchor_node_id IS NOT NULL)
  );

COMMIT;
