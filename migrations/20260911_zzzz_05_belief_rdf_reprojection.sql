-- AIOS character belief reconciliation phase 5.
BEGIN;

-- PostgreSQL is authoritative for belief state; Fuseki is the existing
-- coalesced scope-level projection. Belief reconciliation mutates topology
-- directly in SQL, so explicitly dirty the same scope ledger used by normal
-- topology derivation. runner_v2 will enqueue/project the scope once.

CREATE OR REPLACE FUNCTION aios.mark_belief_scope_dirty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_instance_id uuid;
    v_character_id text;
    v_scope_key text;
BEGIN
    IF TG_OP='DELETE' THEN
        v_instance_id := OLD.instance_id;
    ELSE
        v_instance_id := NEW.instance_id;
    END IF;

    SELECT ci.character_id
    INTO v_character_id
    FROM aios.character_instance ci
    WHERE ci.instance_id=v_instance_id;

    IF v_character_id IS NOT NULL THEN
        v_scope_key := 'char:' || v_character_id;

        INSERT INTO aios.semantic_scope_projection_state (
            scope_key, scope_kind,
            dirty_version, projected_version,
            status, dirty_at, updated_at
        )
        VALUES (
            v_scope_key, 'character',
            1, 0,
            'dirty', now(), now()
        )
        ON CONFLICT (scope_key) DO UPDATE
        SET scope_kind='character',
            dirty_version=aios.semantic_scope_projection_state.dirty_version + 1,
            status='dirty',
            dirty_at=now(),
            last_error=NULL,
            updated_at=now();
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mark_belief_scope_dirty_insert_delete
ON aios.character_belief_state;
CREATE TRIGGER trg_mark_belief_scope_dirty_insert_delete
AFTER INSERT OR DELETE
ON aios.character_belief_state
FOR EACH ROW
EXECUTE FUNCTION aios.mark_belief_scope_dirty();

DROP TRIGGER IF EXISTS trg_mark_belief_scope_dirty_update
ON aios.character_belief_state;
CREATE TRIGGER trg_mark_belief_scope_dirty_update
AFTER UPDATE OF stance, positive_support, negative_support,
    preferred_proposition_id, resolved_through_node_id
ON aios.character_belief_state
FOR EACH ROW
WHEN (
    OLD.stance IS DISTINCT FROM NEW.stance
    OR OLD.positive_support IS DISTINCT FROM NEW.positive_support
    OR OLD.negative_support IS DISTINCT FROM NEW.negative_support
    OR OLD.preferred_proposition_id IS DISTINCT FROM NEW.preferred_proposition_id
    OR OLD.resolved_through_node_id IS DISTINCT FROM NEW.resolved_through_node_id
)
EXECUTE FUNCTION aios.mark_belief_scope_dirty();

-- The backfill migration ran before the dirty-scope triggers existed. Seed or
-- increment one scope-ledger row per character that now has materialized belief
-- state. The projection scheduler will coalesce these into one job per /char.
INSERT INTO aios.semantic_scope_projection_state (
    scope_key, scope_kind,
    dirty_version, projected_version,
    status, dirty_at, updated_at
)
SELECT DISTINCT
    'char:' || ci.character_id,
    'character',
    1,
    0,
    'dirty',
    now(),
    now()
FROM aios.character_belief_state bs
JOIN aios.character_instance ci ON ci.instance_id=bs.instance_id
ON CONFLICT (scope_key) DO UPDATE
SET scope_kind='character',
    dirty_version=aios.semantic_scope_projection_state.dirty_version + 1,
    status='dirty',
    dirty_at=now(),
    last_error=NULL,
    updated_at=now();

COMMIT;
