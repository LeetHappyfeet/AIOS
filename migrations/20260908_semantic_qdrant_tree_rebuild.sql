-- Rebuild derived semantic topology after Qdrant/tree plumbing fixes.
-- 2026-09-08
--
-- This migration invalidates only derived semantic projections and classifier
-- receipts. It does not change proposition identity, source ownership, world
-- assertions, character knowledge, or any epistemic authority.

BEGIN;

-- Existing acquisition projections were built before provenance-first
-- CHARACTER -> SOURCE anchoring. Existing world assertion projections need to
-- run again so SOURCE -> WORLD reports_about anchors can be backfilled.
UPDATE aios.semantic_topology_projection
SET projected_at=NULL,
    updated_at=now(),
    meta=meta || jsonb_build_object(
        'reproject_reason',
        'semantic_qdrant_tree_rebuild_20260908'
    )
WHERE (acquisition_id IS NOT NULL OR assertion_id IS NOT NULL)
  AND projected_at IS NOT NULL;

-- Earlier reconciliation materialized every accepted cluster as a disconnected
-- SEMANTIC_CLUSTER node. Remove only those receipt-owned derived nodes so the
-- reconciler can replace them with typed, structurally-parented pivots.
DELETE FROM aios.semantic_topology_node n
USING aios.semantic_reconciliation_receipt r
WHERE r.source_kind='cluster'
  AND r.action='materialize_semantic_cluster'
  AND r.topology_node_id=n.topology_node_id;

-- Boundary edges may have referenced the old generic cluster nodes. Their edge
-- rows cascade with those nodes; discard the derived receipts so boundaries can
-- be reconciled against the rebuilt pivots.
DELETE FROM aios.semantic_reconciliation_receipt
WHERE source_kind IN ('cluster','boundary');

UPDATE aios.semantic_cluster_classification
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

UPDATE aios.semantic_boundary_classification
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

COMMIT;
