-- Preserve pair-classifier receipts across semantic validation invalidation.
-- Validation owns current trust/staleness; semantic_neighbor_relation records
-- that a particular classifier version evaluated a pair for an embedding version.

BEGIN;

CREATE OR REPLACE FUNCTION aios.mark_semantic_validation_stale(
    p_evidence_type text,
    p_evidence_key text
) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type
          AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    )
    UPDATE aios.semantic_validation_decision v
    SET status='stale', stale_at=COALESCE(v.stale_at, now())
    FROM affected a
    WHERE v.decision_type=a.decision_type
      AND v.decision_key=a.decision_key
      AND v.status <> 'stale';

    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    ), claims AS (
        SELECT DISTINCT v.subject_key
        FROM affected a
        JOIN aios.semantic_validation_decision v
          ON v.decision_type=a.decision_type AND v.decision_key=a.decision_key
        WHERE v.decision_type IN ('semantic_owner','world_assignment','entity_referent')
          AND v.subject_type='claim'
    )
    UPDATE aios.semantic_topology_projection stp
    SET projected_at=NULL,
        updated_at=now(),
        meta=stp.meta || jsonb_build_object('reproject_reason','semantic_validation_stale')
    FROM claims c
    WHERE stp.claim_id::text=c.subject_key
      AND stp.projected_at IS NOT NULL;

    -- Intentionally do not delete semantic_neighbor_relation for stale
    -- proposition_relation/event_identity decisions. The relation is the
    -- durable classifier receipt; semantic_validation_decision is the authority
    -- for whether that interpretation is currently validated.

    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    ), assertions AS (
        SELECT DISTINCT v.subject_key
        FROM affected a
        JOIN aios.semantic_validation_decision v
          ON v.decision_type=a.decision_type AND v.decision_key=a.decision_key
        WHERE v.decision_type='epistemic_promotion'
    )
    UPDATE aios.world_proposition_assertion a
    SET last_checked_at=NULL, updated_at=now()
    FROM assertions s
    WHERE a.assertion_id::text=s.subject_key;
END;
$$;

-- Canonical read surface for consumers that require a presently validated
-- pair interpretation. Raw/advisory consumers may continue reading the base
-- semantic_neighbor_relation table directly.
CREATE OR REPLACE VIEW aios.validated_semantic_neighbor_relation AS
SELECT r.*
FROM aios.semantic_neighbor_relation r
JOIN aios.semantic_validation_decision d
  ON d.decision_type IN ('proposition_relation','event_identity')
 AND d.decision_key=(r.proposition_id::text || ':' || r.neighbor_proposition_id::text)
 AND d.selected_value=r.relation
WHERE d.status <> 'stale';

COMMENT ON VIEW aios.validated_semantic_neighbor_relation IS
'Pair-classifier receipts whose semantic validation decision is currently non-stale. Use this surface for promotion/reconciliation; use semantic_neighbor_relation for raw advisory classifier history.';

-- HUD/debug conflict surfaces represent validated consequences, not raw
-- classifier history. Keep stale CONTRADICTS receipts out while retaining them
-- in semantic_neighbor_relation for classifier fixpoint/provenance.
CREATE OR REPLACE VIEW aios.verified_proposition_conflict AS
SELECT DISTINCT ON (
    LEAST(r.proposition_id::text, r.neighbor_proposition_id::text),
    GREATEST(r.proposition_id::text, r.neighbor_proposition_id::text)
)
    r.proposition_id AS proposition_a_id,
    r.neighbor_proposition_id AS proposition_b_id,
    r.confidence AS strength,
    COALESCE(r.features->>'epistemic_interpretation', 'semantic_conflict_unscoped') AS conflict_type,
    r.features,
    r.classifier_version,
    r.updated_at
FROM aios.validated_semantic_neighbor_relation r
WHERE r.relation='CONTRADICTS'
  AND r.status IN ('candidate','reconciled')
  AND COALESCE((r.features->>'character_conflict_eligible')::boolean, false)
ORDER BY
    LEAST(r.proposition_id::text, r.neighbor_proposition_id::text),
    GREATEST(r.proposition_id::text, r.neighbor_proposition_id::text),
    r.confidence DESC,
    r.updated_at DESC;

COMMENT ON VIEW aios.verified_proposition_conflict IS
'Character-same-viewpoint contradictions independently classified and currently non-stale in semantic validation. Raw classifier receipts remain advisory history.';

COMMIT;
