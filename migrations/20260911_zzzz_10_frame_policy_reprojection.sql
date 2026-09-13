-- Re-run downstream semantic products when frame-policy v3 changes or
-- quarantines an existing interpretation. Raw claims and observations remain
-- durable; only derived projections are invalidated.
BEGIN;

CREATE TEMP TABLE _aios_frame_policy_changed_claims ON COMMIT DROP AS
SELECT DISTINCT f.claim_id
FROM aios.claim_semantic_frame f
WHERE f.decomposer_version='semantic-frame-v2'
  AND f.meta->>'interpretation_policy'='semantic-frame-policy-v3'
  AND (
      f.meta->>'interpretation_status'='ambiguous'
      OR NULLIF(f.meta->>'copular_class','') IS NOT NULL
  );

-- Context classification contains predicate_family/claim_kind, so any copular
-- reclassification must be resolved again before normalization.
UPDATE aios.claim_context_resolution ccr
SET resolver_version='context-resolver-v3-stale-frame-policy'
FROM _aios_frame_policy_changed_claims changed
WHERE ccr.claim_id=changed.claim_id
  AND ccr.resolver_version='context-resolver-v3';

-- Removing only the frame->proposition projection is sufficient to make the
-- normalizer stage eligible after context resolution. The immutable raw claim,
-- observation provenance, and proposition history remain available.
DELETE FROM aios.observation_proposition op
USING aios.observation o, _aios_frame_policy_changed_claims changed
WHERE op.observation_id=o.observation_id
  AND o.claim_id=changed.claim_id;

-- Derived topology must be rebuilt against the newly normalized proposition
-- set. Preserve the projection row as an audit marker rather than deleting it.
UPDATE aios.semantic_topology_projection stp
SET projected_at=NULL,
    meta=COALESCE(stp.meta,'{}'::jsonb) || jsonb_build_object(
        'reproject_reason','semantic_frame_policy_v3'
    )
FROM _aios_frame_policy_changed_claims changed
WHERE stp.claim_id=changed.claim_id;

-- Force the ordinary RDF worker to rewrite affected observation/proposition
-- material and synchronize the current /char belief state.
DELETE FROM aios.rdf_promotion_log rpl
USING _aios_frame_policy_changed_claims changed
WHERE rpl.claim_id=changed.claim_id
  AND (
      (rpl.rdf_dataset='world'
       AND rpl.rdf_graph='urn:aios:world:epistemic'
       AND rpl.rdf_predicate='world:observesProposition')
      OR rpl.rdf_dataset='char'
  );

UPDATE aios.claim_semantic_frame_projection sfp
SET resolved_count=(
        SELECT COUNT(*)
        FROM aios.claim_semantic_frame f
        WHERE f.claim_id=sfp.claim_id
          AND f.decomposer_version=sfp.decomposer_version
          AND f.resolution_status='resolved'
    ),
    projected_at=now(),
    meta=COALESCE(sfp.meta,'{}'::jsonb) || jsonb_build_object(
        'interpretation_policy','semantic-frame-policy-v3',
        'downstream_reprojection_required',true
    )
FROM _aios_frame_policy_changed_claims changed
WHERE sfp.claim_id=changed.claim_id;

COMMIT;
