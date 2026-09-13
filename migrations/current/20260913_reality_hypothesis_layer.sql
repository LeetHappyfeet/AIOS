-- AIOS reality hypothesis layer
--
-- Keeps aios.world as the concrete/runtime authority boundary while allowing
-- propositions to participate in one or more candidate interpretations of
-- reality before anything is admitted as objective world state.

BEGIN;

ALTER TABLE aios.world
    ADD COLUMN IF NOT EXISTS display_name text;

CREATE TABLE IF NOT EXISTS aios.reality_context (
    reality_context_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    context_key text NOT NULL UNIQUE,
    context_kind text NOT NULL DEFAULT 'hypothesis',
    world_id uuid REFERENCES aios.world(world_id) ON DELETE CASCADE,
    parent_context_id uuid REFERENCES aios.reality_context(reality_context_id) ON DELETE SET NULL,
    topic_key text,
    label text,
    status text NOT NULL DEFAULT 'open',
    coherence double precision,
    confidence double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (coherence IS NULL OR coherence BETWEEN 0.0 AND 1.0),
    CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_reality_context_world
    ON aios.reality_context(world_id)
    WHERE world_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reality_context_kind_status
    ON aios.reality_context(context_kind, status);

CREATE INDEX IF NOT EXISTS idx_reality_context_topic
    ON aios.reality_context(topic_key)
    WHERE topic_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS aios.proposition_reality_membership (
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    reality_context_id uuid NOT NULL REFERENCES aios.reality_context(reality_context_id) ON DELETE CASCADE,
    membership_status text NOT NULL DEFAULT 'candidate',
    affinity double precision NOT NULL DEFAULT 0.5,
    confidence double precision NOT NULL DEFAULT 0.5,
    assigned_by text NOT NULL DEFAULT 'reality-resolver-v1',
    validation_decision_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (proposition_id, reality_context_id),
    CHECK (affinity BETWEEN 0.0 AND 1.0),
    CHECK (confidence BETWEEN 0.0 AND 1.0)
);

CREATE INDEX IF NOT EXISTS idx_proposition_reality_context
    ON aios.proposition_reality_membership(reality_context_id, membership_status, affinity DESC);

CREATE TABLE IF NOT EXISTS aios.reality_context_edge (
    from_context_id uuid NOT NULL REFERENCES aios.reality_context(reality_context_id) ON DELETE CASCADE,
    to_context_id uuid NOT NULL REFERENCES aios.reality_context(reality_context_id) ON DELETE CASCADE,
    relation_type text NOT NULL,
    affinity double precision NOT NULL DEFAULT 0.5,
    traversal_cost double precision NOT NULL DEFAULT 0.5,
    confidence double precision NOT NULL DEFAULT 0.5,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (from_context_id, to_context_id, relation_type),
    CHECK (from_context_id <> to_context_id),
    CHECK (affinity BETWEEN 0.0 AND 1.0),
    CHECK (traversal_cost BETWEEN 0.0 AND 1.0),
    CHECK (confidence BETWEEN 0.0 AND 1.0)
);

CREATE INDEX IF NOT EXISTS idx_reality_context_edge_to
    ON aios.reality_context_edge(to_context_id, relation_type);

CREATE TABLE IF NOT EXISTS aios.character_reality_affinity (
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    reality_context_id uuid NOT NULL REFERENCES aios.reality_context(reality_context_id) ON DELETE CASCADE,
    relation_type text NOT NULL DEFAULT 'familiar_with',
    affinity double precision NOT NULL DEFAULT 0.5,
    traversal_cost double precision NOT NULL DEFAULT 0.5,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (character_id, reality_context_id, relation_type),
    CHECK (affinity BETWEEN 0.0 AND 1.0),
    CHECK (traversal_cost BETWEEN 0.0 AND 1.0)
);

CREATE OR REPLACE FUNCTION aios.default_world_display_name(
    p_world_key text,
    p_world_type text,
    p_origin_character_id text
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_world_key = 'liminal' THEN 'Liminal Observation Space'
        WHEN p_origin_character_id IS NOT NULL AND p_world_type = 'character_root'
            THEN COALESCE(
                (SELECT COALESCE(NULLIF(display_name,''), NULLIF(canonical_name,''), character_id)
                   FROM aios.character_identity
                  WHERE character_id=p_origin_character_id),
                p_origin_character_id
            ) || '''s Root World'
        WHEN p_origin_character_id IS NOT NULL AND p_world_type = 'runtime'
            THEN COALESCE(
                (SELECT COALESCE(NULLIF(display_name,''), NULLIF(canonical_name,''), character_id)
                   FROM aios.character_identity
                  WHERE character_id=p_origin_character_id),
                p_origin_character_id
            ) || '''s Runtime World'
        WHEN NULLIF(trim(p_world_key),'') IS NOT NULL
            THEN initcap(regexp_replace(p_world_key, '[:_\-]+', ' ', 'g'))
        ELSE 'Unnamed World'
    END
$$;

CREATE OR REPLACE FUNCTION aios.ensure_world_reality_context(p_world_id uuid)
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
    v_context_id uuid;
    v_world record;
BEGIN
    SELECT reality_context_id
      INTO v_context_id
      FROM aios.reality_context
     WHERE world_id=p_world_id;

    IF v_context_id IS NOT NULL THEN
        RETURN v_context_id;
    END IF;

    SELECT world_id, world_key, world_type, display_name, parent_world_id,
           canon_of_world_id, origin_character_id
      INTO v_world
      FROM aios.world
     WHERE world_id=p_world_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown world_id %', p_world_id;
    END IF;

    INSERT INTO aios.reality_context (
        context_key, context_kind, world_id, label, status,
        coherence, confidence, meta
    )
    VALUES (
        'world:' || p_world_id::text,
        'established_world',
        p_world_id,
        COALESCE(NULLIF(v_world.display_name,''),
                 aios.default_world_display_name(v_world.world_key, v_world.world_type, v_world.origin_character_id)),
        'established',
        1.0,
        1.0,
        jsonb_build_object(
            'world_key', v_world.world_key,
            'world_type', v_world.world_type,
            'authority_boundary', 'aios.world'
        )
    )
    ON CONFLICT (context_key) DO UPDATE
       SET world_id=EXCLUDED.world_id,
           label=COALESCE(aios.reality_context.label, EXCLUDED.label),
           status='established',
           coherence=1.0,
           confidence=1.0,
           updated_at=now(),
           meta=aios.reality_context.meta || EXCLUDED.meta
    RETURNING reality_context_id INTO v_context_id;

    RETURN v_context_id;
END;
$$;

CREATE OR REPLACE FUNCTION aios.sync_world_reality_context()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_context_id uuid;
BEGIN
    IF NEW.display_name IS NULL OR btrim(NEW.display_name) = '' THEN
        NEW.display_name := aios.default_world_display_name(
            NEW.world_key, NEW.world_type, NEW.origin_character_id
        );
    END IF;

    IF TG_OP = 'UPDATE' THEN
        UPDATE aios.reality_context
           SET label=NEW.display_name,
               meta=meta || jsonb_build_object(
                   'world_key', NEW.world_key,
                   'world_type', NEW.world_type
               ),
               updated_at=now()
         WHERE world_id=NEW.world_id;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_world_reality_name_sync ON aios.world;
CREATE TRIGGER trg_world_reality_name_sync
BEFORE INSERT OR UPDATE OF world_key, world_type, origin_character_id, display_name
ON aios.world
FOR EACH ROW
EXECUTE FUNCTION aios.sync_world_reality_context();

CREATE OR REPLACE FUNCTION aios.ensure_new_world_reality_context()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM aios.ensure_world_reality_context(NEW.world_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_ensure_world_reality_context ON aios.world;
CREATE TRIGGER trg_ensure_world_reality_context
AFTER INSERT ON aios.world
FOR EACH ROW
EXECUTE FUNCTION aios.ensure_new_world_reality_context();

UPDATE aios.world
SET display_name=aios.default_world_display_name(world_key, world_type, origin_character_id)
WHERE display_name IS NULL OR btrim(display_name)='';

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT world_id FROM aios.world LOOP
        PERFORM aios.ensure_world_reality_context(r.world_id);
    END LOOP;
END;
$$;

-- Preserve existing world hierarchy as explicit relationships between the
-- corresponding established reality contexts. These are topology links, not
-- proposition truth assertions.
INSERT INTO aios.reality_context_edge (
    from_context_id, to_context_id, relation_type,
    affinity, traversal_cost, confidence, meta
)
SELECT child.reality_context_id,
       parent.reality_context_id,
       'branches_from',
       0.95, 0.05, 1.0,
       jsonb_build_object('source','world.parent_world_id')
FROM aios.world w
JOIN aios.reality_context child ON child.world_id=w.world_id
JOIN aios.reality_context parent ON parent.world_id=w.parent_world_id
WHERE w.parent_world_id IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO aios.reality_context_edge (
    from_context_id, to_context_id, relation_type,
    affinity, traversal_cost, confidence, meta
)
SELECT child.reality_context_id,
       canon.reality_context_id,
       'canon_of',
       0.90, 0.10, 1.0,
       jsonb_build_object('source','world.canon_of_world_id')
FROM aios.world w
JOIN aios.reality_context child ON child.world_id=w.world_id
JOIN aios.reality_context canon ON canon.world_id=w.canon_of_world_id
WHERE w.canon_of_world_id IS NOT NULL
ON CONFLICT DO NOTHING;

-- A character's concrete home world is also its strongest default reality
-- affinity. This is intentionally identity-level; instance-level perspective
-- remains in the existing character/runtime epistemic tables.
INSERT INTO aios.character_reality_affinity (
    character_id, reality_context_id, relation_type,
    affinity, traversal_cost, meta
)
SELECT ci.character_id,
       rc.reality_context_id,
       'home_world',
       1.0, 0.0,
       jsonb_build_object('source','character_identity.home_world_id')
FROM aios.character_identity ci
JOIN aios.reality_context rc ON rc.world_id=ci.home_world_id
WHERE ci.home_world_id IS NOT NULL
ON CONFLICT DO NOTHING;

COMMIT;
