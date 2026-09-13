-- AIOS semantic engine ownership correction
-- Separates semantic ownership from source provenance and invalidates topology
-- scopes built under the previous source-first routing rule.

BEGIN;

ALTER TABLE aios.semantic_topology_node
    DROP CONSTRAINT IF EXISTS semantic_topology_node_scope_kind_check;

ALTER TABLE aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_scope_kind_check
    CHECK (scope_kind IN ('character','world','source','unresolved'));

-- Any source scope containing claims that the context resolver already classified
-- as character, narrative, or speaker semantics was produced by the old
-- source-first topology rule. Remove the whole affected source scope so shared
-- ROOT/TOPIC nodes cannot survive as stale semantic ownership artifacts.
WITH affected_scopes AS (
    SELECT DISTINCT stp.scope_key
    FROM aios.semantic_topology_projection stp
    JOIN aios.claim_context_resolution ccr ON ccr.claim_id=stp.claim_id
    WHERE stp.claim_id IS NOT NULL
      AND stp.scope_key LIKE 'source:%'
      AND ccr.epistemic_scope IN ('character','narrative','speaker')
), deleted_projections AS (
    DELETE FROM aios.semantic_topology_projection stp
    USING affected_scopes s
    WHERE stp.scope_key=s.scope_key
      AND stp.claim_id IS NOT NULL
    RETURNING stp.scope_key
)
DELETE FROM aios.semantic_topology_node n
USING affected_scopes s
WHERE n.scope_key=s.scope_key;

-- Also invalidate any remaining claim projection whose semantic scope should be
-- reconsidered by the hybrid ownership resolver. The normal supervisor will
-- enqueue these claims again because their completed topology projection is gone.
DELETE FROM aios.semantic_topology_projection stp
USING aios.claim_context_resolution ccr
WHERE stp.claim_id=ccr.claim_id
  AND ccr.epistemic_scope IN ('character','narrative','speaker');

-- Vector geometry is asynchronous relative to claim topology. When a new
-- semantic neighbor arrives, only unresolved claim projections touching either
-- proposition are marked stale. The normal topology supervisor will re-run the
-- hybrid resolver. Resolved character/world/source ownership is never reopened
-- merely because a vector neighbor appeared.
CREATE OR REPLACE FUNCTION aios.reconsider_unresolved_topology_from_neighbor()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE aios.semantic_topology_projection stp
    SET projected_at=NULL,
        updated_at=now(),
        meta=stp.meta || jsonb_build_object(
            'reproject_reason', 'semantic_neighbor_evidence',
            'neighbor_embedding_version', NEW.embedding_version
        )
    FROM aios.observation o
    WHERE stp.claim_id=o.claim_id
      AND stp.projected_at IS NOT NULL
      AND stp.scope_key=('unresolved:' || o.claim_id::text)
      AND o.proposition_id IN (NEW.proposition_id, NEW.neighbor_proposition_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_reconsider_unresolved_topology_from_neighbor
    ON aios.semantic_neighbor_candidate;

CREATE TRIGGER trg_reconsider_unresolved_topology_from_neighbor
AFTER INSERT OR UPDATE OF similarity, status
ON aios.semantic_neighbor_candidate
FOR EACH ROW
EXECUTE FUNCTION aios.reconsider_unresolved_topology_from_neighbor();

COMMIT;
