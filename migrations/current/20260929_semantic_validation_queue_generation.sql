-- Preserve queue generations when evidence changes during validation.
-- A conflict refreshes enqueued_at so a worker only deletes the generation it
-- actually processed.

BEGIN;

CREATE OR REPLACE FUNCTION aios.enqueue_semantic_relation_validation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    pair_key text;
    dtype text;
BEGIN
    pair_key := NEW.proposition_id::text || ':' || NEW.neighbor_proposition_id::text;
    dtype := CASE
        WHEN NEW.relation='SAME_EVENT'
          OR COALESCE((NEW.features->>'both_events')::boolean, false)
        THEN 'event_identity'
        ELSE 'proposition_relation'
    END;

    INSERT INTO aios.semantic_relation_validation_queue(
        decision_type, decision_key, enqueued_at, reason
    )
    VALUES (dtype, pair_key, now(), 'classifier_receipt')
    ON CONFLICT (decision_type, decision_key) DO UPDATE
    SET enqueued_at=EXCLUDED.enqueued_at,
        reason=EXCLUDED.reason;

    RETURN NEW;
END;
$$;

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
    ), stale_pairs AS (
        UPDATE aios.semantic_validation_decision v
        SET status='stale', stale_at=COALESCE(v.stale_at, now())
        FROM affected a
        WHERE v.decision_type=a.decision_type
          AND v.decision_key=a.decision_key
          AND v.status <> 'stale'
        RETURNING v.decision_type, v.decision_key
    )
    INSERT INTO aios.semantic_relation_validation_queue(
        decision_type, decision_key, enqueued_at, reason
    )
    SELECT decision_type, decision_key, now(), 'evidence_stale'
    FROM stale_pairs
    WHERE decision_type IN ('proposition_relation','event_identity')
    ON CONFLICT (decision_type, decision_key) DO UPDATE
    SET enqueued_at=EXCLUDED.enqueued_at,
        reason='evidence_stale';

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

COMMIT;
