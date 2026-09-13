-- AIOS shared-world timeline topology
--
-- Makes the world, rather than the character, the root of objective temporal
-- history.  Character timelines remain subjective/per-perspective DAGs and are
-- linked to one shared objective timeline for the world they inhabit.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.world_timeline_binding (
    world_id uuid PRIMARY KEY REFERENCES aios.world(world_id) ON DELETE CASCADE,
    objective_timeline_id uuid NOT NULL UNIQUE REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS aios.character_world_timeline_link (
    character_timeline_id uuid PRIMARY KEY REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    world_timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    relation text NOT NULL DEFAULT 'inhabits',
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (character_timeline_id <> world_timeline_id),
    CHECK (relation IN ('inhabits', 'observes', 'projects_from'))
);

CREATE INDEX IF NOT EXISTS idx_character_world_timeline_world
    ON aios.character_world_timeline_link (world_id, world_timeline_id);

CREATE TABLE IF NOT EXISTS aios.world_event_exposure (
    exposure_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    world_timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    world_node_id uuid NOT NULL REFERENCES aios.dag_node(node_id) ON DELETE CASCADE,
    character_timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    character_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    acquisition_id uuid REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE SET NULL,
    exposure_kind text NOT NULL,
    confidence double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    CHECK (exposure_kind IN (
        'observed', 'participated', 'caused', 'heard_about',
        'inferred_from', 'remembered_from', 'sensor', 'system'
    ))
);

CREATE INDEX IF NOT EXISTS idx_world_event_exposure_instance
    ON aios.world_event_exposure (instance_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_world_event_exposure_world_node
    ON aios.world_event_exposure (world_node_id, instance_id);

CREATE OR REPLACE FUNCTION aios.ensure_world_objective_timeline(p_world_id uuid)
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
    v_timeline_id uuid;
BEGIN
    SELECT b.objective_timeline_id
      INTO v_timeline_id
      FROM aios.world_timeline_binding b
     WHERE b.world_id = p_world_id;

    IF v_timeline_id IS NOT NULL THEN
        RETURN v_timeline_id;
    END IF;

    SELECT t.timeline_id
      INTO v_timeline_id
      FROM aios.timeline t
     WHERE t.world_id = p_world_id
       AND COALESCE(t.meta->>'timeline_role', '') = 'objective'
     ORDER BY t.created_at, t.timeline_id
     LIMIT 1;

    IF v_timeline_id IS NULL THEN
        INSERT INTO aios.timeline (
            world_id, name, session_id, character_id, user_name,
            scope_key, meta, source_id
        )
        VALUES (
            p_world_id, 'world', NULL, NULL, NULL,
            'world',
            jsonb_build_object(
                'timeline_role', 'objective',
                'shared_world', true,
                'authority', 'world'
            ),
            NULL
        )
        RETURNING timeline_id INTO v_timeline_id;
    END IF;

    INSERT INTO aios.world_timeline_binding (
        world_id, objective_timeline_id, meta
    )
    VALUES (
        p_world_id,
        v_timeline_id,
        jsonb_build_object('source', 'shared-world-topology-v1')
    )
    ON CONFLICT (world_id) DO UPDATE
       SET objective_timeline_id = EXCLUDED.objective_timeline_id,
           meta = aios.world_timeline_binding.meta || EXCLUDED.meta;

    RETURN v_timeline_id;
END;
$$;

CREATE OR REPLACE FUNCTION aios.link_character_timeline_to_world()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_world_timeline_id uuid;
BEGIN
    -- Objective timelines are world-owned and must never point back to themselves.
    IF NEW.character_id IS NULL
       OR COALESCE(NEW.meta->>'timeline_role', '') = 'objective' THEN
        RETURN NEW;
    END IF;

    v_world_timeline_id := aios.ensure_world_objective_timeline(NEW.world_id);

    INSERT INTO aios.character_world_timeline_link (
        character_timeline_id,
        world_id,
        world_timeline_id,
        relation,
        meta
    )
    VALUES (
        NEW.timeline_id,
        NEW.world_id,
        v_world_timeline_id,
        'inhabits',
        jsonb_build_object(
            'source', 'timeline-trigger',
            'character_id', NEW.character_id,
            'session_id', NEW.session_id
        )
    )
    ON CONFLICT (character_timeline_id) DO UPDATE
       SET world_id = EXCLUDED.world_id,
           world_timeline_id = EXCLUDED.world_timeline_id,
           relation = EXCLUDED.relation,
           meta = aios.character_world_timeline_link.meta || EXCLUDED.meta;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_link_character_timeline_to_world ON aios.timeline;
CREATE TRIGGER trg_link_character_timeline_to_world
AFTER INSERT OR UPDATE OF world_id, character_id, meta
ON aios.timeline
FOR EACH ROW
EXECUTE FUNCTION aios.link_character_timeline_to_world();

CREATE OR REPLACE FUNCTION aios.ensure_new_world_objective_timeline()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM aios.ensure_world_objective_timeline(NEW.world_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_ensure_world_objective_timeline ON aios.world;
CREATE TRIGGER trg_ensure_world_objective_timeline
AFTER INSERT ON aios.world
FOR EACH ROW
EXECUTE FUNCTION aios.ensure_new_world_objective_timeline();

CREATE OR REPLACE FUNCTION aios.validate_world_event_exposure()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_world_node_timeline uuid;
    v_character_node_timeline uuid;
    v_link_world_id uuid;
    v_link_world_timeline uuid;
BEGIN
    SELECT timeline_id INTO v_world_node_timeline
      FROM aios.dag_node
     WHERE node_id = NEW.world_node_id;

    IF v_world_node_timeline IS DISTINCT FROM NEW.world_timeline_id THEN
        RAISE EXCEPTION 'world_node_id % is not on world_timeline_id %',
            NEW.world_node_id, NEW.world_timeline_id;
    END IF;

    IF NEW.character_node_id IS NOT NULL THEN
        SELECT timeline_id INTO v_character_node_timeline
          FROM aios.dag_node
         WHERE node_id = NEW.character_node_id;

        IF v_character_node_timeline IS DISTINCT FROM NEW.character_timeline_id THEN
            RAISE EXCEPTION 'character_node_id % is not on character_timeline_id %',
                NEW.character_node_id, NEW.character_timeline_id;
        END IF;
    END IF;

    SELECT world_id, world_timeline_id
      INTO v_link_world_id, v_link_world_timeline
      FROM aios.character_world_timeline_link
     WHERE character_timeline_id = NEW.character_timeline_id;

    IF v_link_world_id IS DISTINCT FROM NEW.world_id
       OR v_link_world_timeline IS DISTINCT FROM NEW.world_timeline_id THEN
        RAISE EXCEPTION 'character timeline % is not linked to world timeline % in world %',
            NEW.character_timeline_id, NEW.world_timeline_id, NEW.world_id;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_world_event_exposure ON aios.world_event_exposure;
CREATE TRIGGER trg_validate_world_event_exposure
BEFORE INSERT OR UPDATE
ON aios.world_event_exposure
FOR EACH ROW
EXECUTE FUNCTION aios.validate_world_event_exposure();

-- Backfill one objective timeline for every existing world.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT world_id FROM aios.world LOOP
        PERFORM aios.ensure_world_objective_timeline(r.world_id);
    END LOOP;
END;
$$;

-- Backfill links for all existing character-scoped timelines.  This includes
-- legacy character-private runtime worlds; those simply become private worlds
-- with the same objective/subjective split as shared worlds.
INSERT INTO aios.character_world_timeline_link (
    character_timeline_id,
    world_id,
    world_timeline_id,
    relation,
    meta
)
SELECT
    t.timeline_id,
    t.world_id,
    b.objective_timeline_id,
    'inhabits',
    jsonb_build_object(
        'source', 'shared-world-topology-backfill-v1',
        'character_id', t.character_id,
        'session_id', t.session_id
    )
FROM aios.timeline t
JOIN aios.world_timeline_binding b ON b.world_id = t.world_id
WHERE t.character_id IS NOT NULL
  AND COALESCE(t.meta->>'timeline_role', '') <> 'objective'
ON CONFLICT (character_timeline_id) DO UPDATE
   SET world_id = EXCLUDED.world_id,
       world_timeline_id = EXCLUDED.world_timeline_id,
       relation = EXCLUDED.relation,
       meta = aios.character_world_timeline_link.meta || EXCLUDED.meta;

-- Reclassify the old character-root language without breaking identifiers.
-- These worlds remain valid as backwards-compatible private fallback worlds.
UPDATE aios.world
SET meta = meta || jsonb_build_object(
        'topology_role', 'private_fallback_world',
        'shared_world_capable', true
    )
WHERE world_type = 'character_root'
  AND COALESCE(meta->>'topology_role', '') = 'personal_universe_root';

COMMIT;
