-- Character-root and DAG-anchored world topology
-- 2026-09-06
BEGIN;

ALTER TABLE aios.world
    ADD COLUMN IF NOT EXISTS root_world_id uuid REFERENCES aios.world(world_id),
    ADD COLUMN IF NOT EXISTS anchor_timeline_id uuid REFERENCES aios.timeline(timeline_id),
    ADD COLUMN IF NOT EXISTS anchor_node_id uuid REFERENCES aios.dag_node(node_id),
    ADD COLUMN IF NOT EXISTS origin_character_id text REFERENCES aios.character_identity(character_id);

CREATE INDEX IF NOT EXISTS idx_world_root_world_id
    ON aios.world(root_world_id);

CREATE INDEX IF NOT EXISTS idx_world_anchor_node_id
    ON aios.world(anchor_node_id);

CREATE INDEX IF NOT EXISTS idx_world_origin_character_id
    ON aios.world(origin_character_id);

CREATE TABLE IF NOT EXISTS aios.world_rdf_projection (
    world_id uuid PRIMARY KEY REFERENCES aios.world(world_id) ON DELETE CASCADE,
    rdf_graph text NOT NULL DEFAULT 'urn:aios:world:topology',
    projected_at timestamptz,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Backfill one stable personal-universe root for every existing character that
-- does not already have a home world.
INSERT INTO aios.world (
    world_key,
    world_type,
    origin_character_id,
    meta
)
SELECT
    'char:' || ci.character_id || ':root',
    'character_root',
    ci.character_id,
    jsonb_build_object(
        'source', 'character_identity',
        'topology_role', 'personal_universe_root'
    )
FROM aios.character_identity ci
WHERE ci.home_world_id IS NULL
ON CONFLICT (world_key) DO NOTHING;

UPDATE aios.world w
SET root_world_id = w.world_id
WHERE w.world_type = 'character_root'
  AND w.root_world_id IS NULL;

UPDATE aios.character_identity ci
SET home_world_id = w.world_id,
    updated_at = now()
FROM aios.world w
WHERE ci.home_world_id IS NULL
  AND w.world_key = 'char:' || ci.character_id || ':root';

COMMIT;
