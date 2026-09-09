-- Separate semantic TOPIC grouping nodes from proposition identity leaves.
-- 2026-09-09
--
-- Older topology stored proposition_id directly on a TOPIC node keyed only by
-- (scope_key, topic_key). Multiple propositions sharing a topic therefore
-- collapsed onto one topology node, preventing pairwise semantic relations
-- such as SAME_TOPIC, REFINES and CONTRADICTS from being materialized safely.

BEGIN;

-- TOPIC is now a grouping/pivot node only. Proposition identity is represented
-- by a dedicated PROPOSITION child keyed by proposition_id.
UPDATE aios.semantic_topology_node
SET proposition_id=NULL,
    claim_id=NULL,
    assertion_id=NULL,
    acquisition_id=NULL,
    dag_node_id=NULL,
    meta=meta || jsonb_build_object(
        'semantic_role',
        'topic_group',
        'rebuild_reason',
        'semantic_proposition_leaves_20260909'
    ),
    updated_at=now()
WHERE node_type='TOPIC';

-- Remove only classifier-derived pivot nodes from previous reconciliation.
-- They will be recreated and linked to the new proposition leaves.
DELETE FROM aios.semantic_topology_node n
USING aios.semantic_reconciliation_receipt r
WHERE r.source_kind='cluster'
  AND r.topology_node_id=n.topology_node_id;

-- Pair/boundary/cluster receipts all target the old topology representation.
DELETE FROM aios.semantic_reconciliation_receipt;

UPDATE aios.semantic_neighbor_relation
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

UPDATE aios.semantic_cluster_classification
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

UPDATE aios.semantic_boundary_classification
SET status='candidate',
    updated_at=now()
WHERE status='reconciled';

-- Re-run deterministic topology derivation idempotently. Existing ROOT, INSTANCE,
-- branch and TOPIC nodes are reused; new PROPOSITION leaves are added beneath
-- their topic groups.
UPDATE aios.semantic_topology_projection
SET projected_at=NULL,
    updated_at=now(),
    meta=meta || jsonb_build_object(
        'reproject_reason',
        'semantic_proposition_leaves_20260909'
    )
WHERE projected_at IS NOT NULL;

COMMIT;
