-- Link successive compatible runtime session worlds without merging them.
-- The relationship feeds the existing materialized world-resolution cache, so
-- the live HUD path remains a flat indexed lookup.

BEGIN;

ALTER TABLE aios.world_relation
    DROP CONSTRAINT IF EXISTS world_relation_relation_check;
ALTER TABLE aios.world_relation
    ADD CONSTRAINT world_relation_relation_check
    CHECK (relation IN ('inherits', 'derived_from', 'counterpart_of', 'continues'));

CREATE OR REPLACE FUNCTION aios.link_runtime_world_continuity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    predecessor uuid;
BEGIN
    IF NEW.world_type <> 'runtime'
       OR NEW.origin_character_id IS NULL
       OR NEW.root_world_id IS NULL
       OR COALESCE(NEW.meta->>'continuity_mode', 'auto') = 'isolated'
    THEN
        RETURN NEW;
    END IF;

    SELECT w.world_id
    INTO predecessor
    FROM aios.world w
    WHERE w.world_type = 'runtime'
      AND w.world_id <> NEW.world_id
      AND w.origin_character_id = NEW.origin_character_id
      AND w.root_world_id = NEW.root_world_id
      AND COALESCE(w.meta->>'continuity_mode', 'auto') <> 'isolated'
      AND w.created_at <= NEW.created_at
    ORDER BY w.created_at DESC, w.world_id DESC
    LIMIT 1;

    IF predecessor IS NULL THEN
        RETURN NEW;
    END IF;

    INSERT INTO aios.world_relation(
        world_id, source_world_id, relation, priority, domains, enabled, meta
    ) VALUES (
        NEW.world_id,
        predecessor,
        'continues',
        10,
        ARRAY['general','history']::text[],
        true,
        jsonb_build_object(
            'source','runtime-session-continuity-v1',
            'compatibility','same_character_root',
            'automatic',true
        )
    )
    ON CONFLICT (world_id, source_world_id, relation) DO NOTHING;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_link_runtime_world_continuity ON aios.world;
CREATE TRIGGER trg_link_runtime_world_continuity
AFTER INSERT ON aios.world
FOR EACH ROW EXECUTE FUNCTION aios.link_runtime_world_continuity();

-- Backfill only adjacent runtime worlds within the same persistent character
-- root. Existing worlds explicitly marked continuity_mode=isolated are excluded.
WITH ordered AS (
    SELECT
        w.world_id,
        lag(w.world_id) OVER (
            PARTITION BY w.origin_character_id, w.root_world_id
            ORDER BY w.created_at, w.world_id
        ) AS predecessor
    FROM aios.world w
    WHERE w.world_type='runtime'
      AND w.origin_character_id IS NOT NULL
      AND w.root_world_id IS NOT NULL
      AND COALESCE(w.meta->>'continuity_mode', 'auto') <> 'isolated'
)
INSERT INTO aios.world_relation(
    world_id, source_world_id, relation, priority, domains, enabled, meta
)
SELECT
    world_id,
    predecessor,
    'continues',
    10,
    ARRAY['general','history']::text[],
    true,
    jsonb_build_object(
        'source','runtime-session-continuity-v1',
        'compatibility','same_character_root',
        'automatic',true,
        'backfill',true
    )
FROM ordered
WHERE predecessor IS NOT NULL
ON CONFLICT (world_id, source_world_id, relation) DO NOTHING;

-- Ensure caches are populated for runtime worlds even if trigger ordering or a
-- pre-existing relation meant the relation trigger did not rebuild one.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT world_id FROM aios.world WHERE world_type='runtime'
    LOOP
        PERFORM aios.rebuild_world_resolution_cache(r.world_id);
    END LOOP;
END;
$$;

COMMIT;
