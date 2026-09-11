-- AIOS character belief reconciliation phase 5.
BEGIN;

-- PostgreSQL is authoritative for belief state; RDF is a derived projection.
-- Reuse the existing acquisition-topology reprojection path instead of adding
-- a second RDF worker. If an unprojected acquisition already exists for the
-- same character, that job will rewrite the full /char scope. Otherwise mark
-- one existing acquisition projection stale so the normal supervisor does it.

CREATE OR REPLACE FUNCTION aios.queue_belief_scope_reprojection()
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

    IF v_character_id IS NULL THEN
        IF TG_OP='DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    v_scope_key := 'char:' || v_character_id;

    -- A pending acquisition topology projection will rewrite the entire scope,
    -- including BELIEF_STATE nodes and support/opposition edges, so avoid
    -- manufacturing a redundant stale projection in that case.
    IF NOT EXISTS (
        SELECT 1
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.character_instance ci
          ON ci.instance_id=kae.instance_id
        WHERE ci.character_id=v_character_id
          AND kae.proposition_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM aios.semantic_topology_projection stp
              WHERE stp.acquisition_id=kae.acquisition_id
                AND stp.projected_at IS NOT NULL
                AND stp.resolver_version='semantic-topology-v1'
          )
    ) THEN
        UPDATE aios.semantic_topology_projection stp
        SET projected_at=NULL,
            updated_at=now(),
            meta=stp.meta || jsonb_build_object(
                'reproject_reason',
                'character_belief_state_changed'
            )
        WHERE stp.projection_key = (
            SELECT candidate.projection_key
            FROM aios.semantic_topology_projection candidate
            WHERE candidate.scope_key=v_scope_key
              AND candidate.acquisition_id IS NOT NULL
              AND candidate.projected_at IS NOT NULL
              AND candidate.resolver_version='semantic-topology-v1'
            ORDER BY candidate.updated_at DESC, candidate.projection_key
            LIMIT 1
        );
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_queue_belief_rdf_insert_delete
ON aios.character_belief_state;
CREATE TRIGGER trg_queue_belief_rdf_insert_delete
AFTER INSERT OR DELETE
ON aios.character_belief_state
FOR EACH ROW
EXECUTE FUNCTION aios.queue_belief_scope_reprojection();

DROP TRIGGER IF EXISTS trg_queue_belief_rdf_update
ON aios.character_belief_state;
CREATE TRIGGER trg_queue_belief_rdf_update
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
EXECUTE FUNCTION aios.queue_belief_scope_reprojection();

-- The backfill migration ran before these triggers existed. Mark one completed
-- acquisition projection per character scope stale so existing databases get a
-- single full-scope RDF refresh after upgrade.
WITH latest AS (
    SELECT DISTINCT ON (stp.scope_key)
        stp.projection_key
    FROM aios.semantic_topology_projection stp
    WHERE stp.scope_key LIKE 'char:%'
      AND stp.acquisition_id IS NOT NULL
      AND stp.projected_at IS NOT NULL
      AND stp.resolver_version='semantic-topology-v1'
      AND EXISTS (
          SELECT 1
          FROM aios.character_belief_state bs
          JOIN aios.character_instance ci ON ci.instance_id=bs.instance_id
          WHERE stp.scope_key='char:' || ci.character_id
      )
    ORDER BY stp.scope_key, stp.updated_at DESC, stp.projection_key
)
UPDATE aios.semantic_topology_projection stp
SET projected_at=NULL,
    updated_at=now(),
    meta=stp.meta || jsonb_build_object(
        'reproject_reason',
        'character_belief_state_backfill'
    )
FROM latest
WHERE stp.projection_key=latest.projection_key;

COMMIT;
