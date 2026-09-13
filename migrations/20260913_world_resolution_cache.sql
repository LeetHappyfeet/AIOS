-- Materialized world/domain resolution for fast live retrieval.
-- Rich world relations are evaluated only when configuration changes. The HUD
-- path reads a flat indexed cache and never performs recursive graph traversal.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.world_relation (
    relation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    source_world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    relation text NOT NULL DEFAULT 'inherits',
    priority integer NOT NULL DEFAULT 100,
    domains text[] NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (world_id <> source_world_id),
    CHECK (relation IN ('inherits', 'derived_from', 'counterpart_of')),
    CHECK (cardinality(domains) BETWEEN 1 AND 16),
    UNIQUE (world_id, source_world_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_world_relation_child
    ON aios.world_relation (world_id) WHERE enabled;
CREATE INDEX IF NOT EXISTS idx_world_relation_source
    ON aios.world_relation (source_world_id) WHERE enabled;

CREATE TABLE IF NOT EXISTS aios.world_domain_policy (
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    domain text NOT NULL,
    inheritance_mode text NOT NULL DEFAULT 'inherit',
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (world_id, domain),
    CHECK (inheritance_mode IN ('inherit', 'local_only')),
    CHECK (domain = lower(domain)),
    CHECK (domain ~ '^[a-z][a-z0-9_]{0,31}$')
);

CREATE TABLE IF NOT EXISTS aios.world_resolution_cache (
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    domain text NOT NULL,
    source_world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    priority integer NOT NULL,
    depth smallint NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (world_id, domain, source_world_id),
    CHECK (depth BETWEEN 1 AND 4)
);

CREATE INDEX IF NOT EXISTS idx_world_resolution_live_lookup
    ON aios.world_resolution_cache (world_id, domain, priority, source_world_id);

CREATE OR REPLACE FUNCTION aios.rebuild_world_resolution_cache(p_world_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM aios.world_resolution_cache WHERE world_id = p_world_id;

    WITH RECURSIVE walk AS (
        SELECT
            r.world_id AS root_world_id,
            d.domain,
            r.source_world_id,
            1::smallint AS depth,
            (100000 + r.priority)::integer AS rank,
            ARRAY[r.world_id, r.source_world_id]::uuid[] AS path
        FROM aios.world_relation r
        CROSS JOIN LATERAL (
            SELECT lower(trim(x)) AS domain
            FROM unnest(r.domains) AS x
            WHERE lower(trim(x)) ~ '^[a-z][a-z0-9_]{0,31}$'
        ) d
        WHERE r.world_id = p_world_id
          AND r.enabled
          AND NOT EXISTS (
              SELECT 1
              FROM aios.world_domain_policy p
              WHERE p.world_id = r.world_id
                AND p.domain = d.domain
                AND p.inheritance_mode = 'local_only'
          )

        UNION ALL

        SELECT
            w.root_world_id,
            w.domain,
            r.source_world_id,
            (w.depth + 1)::smallint,
            (w.rank + 100000 + r.priority)::integer,
            w.path || r.source_world_id
        FROM walk w
        JOIN aios.world_relation r
          ON r.world_id = w.source_world_id
         AND r.enabled
         AND w.domain = ANY (
             SELECT lower(trim(x)) FROM unnest(r.domains) AS x
         )
        WHERE w.depth < 4
          AND NOT (r.source_world_id = ANY(w.path))
          AND NOT EXISTS (
              SELECT 1
              FROM aios.world_domain_policy p
              WHERE p.world_id = w.source_world_id
                AND p.domain = w.domain
                AND p.inheritance_mode = 'local_only'
          )
    ), ranked AS (
        SELECT
            root_world_id AS world_id,
            domain,
            source_world_id,
            MIN(rank) AS priority,
            MIN(depth) AS depth
        FROM walk
        GROUP BY root_world_id, domain, source_world_id
    )
    INSERT INTO aios.world_resolution_cache (
        world_id, domain, source_world_id, priority, depth, meta
    )
    SELECT
        world_id,
        domain,
        source_world_id,
        priority,
        depth,
        jsonb_build_object('source', 'world-resolution-cache-v1')
    FROM ranked;
END;
$$;

CREATE OR REPLACE FUNCTION aios.refresh_world_resolution_dependents(p_world_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN
        WITH RECURSIVE affected(world_id, depth, path) AS (
            SELECT p_world_id, 0, ARRAY[p_world_id]::uuid[]
            UNION ALL
            SELECT rel.world_id, a.depth + 1, a.path || rel.world_id
            FROM affected a
            JOIN aios.world_relation rel
              ON rel.source_world_id = a.world_id
             AND rel.enabled
            WHERE a.depth < 4
              AND NOT (rel.world_id = ANY(a.path))
        )
        SELECT DISTINCT world_id FROM affected
    LOOP
        PERFORM aios.rebuild_world_resolution_cache(r.world_id);
    END LOOP;
END;
$$;

-- Live-path API. This function performs only indexed lookups over the flat
-- cache. The current world always wins and is returned with priority zero.
CREATE OR REPLACE FUNCTION aios.get_world_resolution_scope(
    p_world_id uuid,
    p_domain text
)
RETURNS TABLE (
    source_world_id uuid,
    priority integer,
    depth smallint
)
LANGUAGE sql
STABLE
AS $$
    SELECT p_world_id, 0, 0::smallint
    UNION ALL
    SELECT c.source_world_id, c.priority, c.depth
    FROM aios.world_resolution_cache c
    WHERE c.world_id = p_world_id
      AND c.domain = lower(p_domain)
    ORDER BY 2, 3, 1;
$$;

CREATE OR REPLACE FUNCTION aios.refresh_world_resolution_from_relation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM aios.refresh_world_resolution_dependents(OLD.world_id);
        RETURN OLD;
    END IF;

    PERFORM aios.refresh_world_resolution_dependents(NEW.world_id);
    IF TG_OP = 'UPDATE' AND OLD.world_id IS DISTINCT FROM NEW.world_id THEN
        PERFORM aios.refresh_world_resolution_dependents(OLD.world_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_world_resolution_relation ON aios.world_relation;
CREATE TRIGGER trg_refresh_world_resolution_relation
AFTER INSERT OR UPDATE OR DELETE ON aios.world_relation
FOR EACH ROW EXECUTE FUNCTION aios.refresh_world_resolution_from_relation();

CREATE OR REPLACE FUNCTION aios.refresh_world_resolution_from_policy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM aios.refresh_world_resolution_dependents(OLD.world_id);
        RETURN OLD;
    END IF;

    PERFORM aios.refresh_world_resolution_dependents(NEW.world_id);
    IF TG_OP = 'UPDATE' AND OLD.world_id IS DISTINCT FROM NEW.world_id THEN
        PERFORM aios.refresh_world_resolution_dependents(OLD.world_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_world_resolution_policy ON aios.world_domain_policy;
CREATE TRIGGER trg_refresh_world_resolution_policy
AFTER INSERT OR UPDATE OR DELETE ON aios.world_domain_policy
FOR EACH ROW EXECUTE FUNCTION aios.refresh_world_resolution_from_policy();

-- Existing worlds are local-only until an explicit relation is added. This is
-- deliberately conservative: sharing a name such as America does not imply
-- that every real historical event is canon in a fictional world.

COMMIT;
