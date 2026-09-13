-- Refine semantic retention policy after historical dry-run validation.
--
-- A missing semantic predicate is an extraction failure, not proof that the
-- underlying evidence is useless. Preserve that evidence as ACTIVE/unresolved
-- so it remains available for future repair/re-extraction. Structural parser
-- leakage and exact same-event duplicates remain eligible for quarantine.
BEGIN;

CREATE OR REPLACE FUNCTION aios.evaluate_acquisition_retention(
    p_acquisition_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_status text;
    v_reason text;
    v_instance_id uuid;
    v_proposition_id uuid;
    v_claim_id uuid;
    v_dag_node_id uuid;
    v_duplicate_id uuid;
BEGIN
    SELECT kae.instance_id,kae.proposition_id,kae.claim_id,kae.dag_node_id,
           sea.status,sea.reason
    INTO v_instance_id,v_proposition_id,v_claim_id,v_dag_node_id,v_status,v_reason
    FROM aios.knowledge_acquisition_event kae
    LEFT JOIN aios.semantic_evidence_admission sea
      ON sea.acquisition_id=kae.acquisition_id
    WHERE kae.acquisition_id=p_acquisition_id;

    IF v_instance_id IS NULL THEN
        RETURN;
    END IF;

    -- Only structural parser leakage is automatic extraction trash.
    -- missing_semantic_predicate deliberately stays out of this list: an
    -- incomplete proposition can still be valuable episodic/source evidence
    -- and should remain available for semantic repair.
    IF v_reason IN (
        'internal_frame_reference',
        'serialized_semantic_component'
    ) THEN
        PERFORM aios.set_semantic_retention_state(
            'knowledge_acquisition_event',p_acquisition_id::text,'QUARANTINED',
            'extraction_debris:' || v_reason,
            0.05,0.05,0.0,1.0,
            jsonb_build_object('claim_id',v_claim_id,'admission_reason',v_reason)
        );
        IF v_claim_id IS NOT NULL THEN
            PERFORM aios.set_semantic_retention_state(
                'claim_candidate',v_claim_id::text,'QUARANTINED',
                'extraction_debris:' || v_reason,
                0.05,0.05,0.0,1.0,
                jsonb_build_object('acquisition_id',p_acquisition_id)
            );
        END IF;
        RETURN;
    END IF;

    -- Exact duplicate suppression remains scoped to one character instance,
    -- one proposition, and one DAG generation coordinate. Keep the earliest
    -- ACTIVE representative. semantic_retention_protection() can still veto
    -- the quarantine if current belief state depends on this acquisition.
    SELECT other.acquisition_id
    INTO v_duplicate_id
    FROM aios.knowledge_acquisition_event other
    LEFT JOIN aios.semantic_retention_state rs
      ON rs.artifact_type='knowledge_acquisition_event'
     AND rs.artifact_id=other.acquisition_id::text
    WHERE other.instance_id=v_instance_id
      AND other.proposition_id=v_proposition_id
      AND other.dag_node_id IS NOT DISTINCT FROM v_dag_node_id
      AND other.acquisition_id<>p_acquisition_id
      AND COALESCE(rs.state,'ACTIVE')='ACTIVE'
      AND other.created_at <= (
          SELECT created_at
          FROM aios.knowledge_acquisition_event
          WHERE acquisition_id=p_acquisition_id
      )
    ORDER BY other.created_at,other.acquisition_id
    LIMIT 1;

    IF v_duplicate_id IS NOT NULL THEN
        PERFORM aios.set_semantic_retention_state(
            'knowledge_acquisition_event',p_acquisition_id::text,'QUARANTINED',
            'duplicate_same_event_proposition',
            0.10,0.90,1.0,1.0,
            jsonb_build_object('representative_acquisition_id',v_duplicate_id)
        );
        RETURN;
    END IF;
END;
$$;

COMMENT ON FUNCTION aios.evaluate_acquisition_retention(uuid) IS
'Phase-1 retention evaluator. Quarantines structural parser leakage and safe exact duplicates; incomplete semantic extraction remains active evidence for later repair.';

-- Repair any rows that may have been automatically quarantined by the previous
-- policy since Phase 1 was installed. Use the transition API so admission is
-- recomputed and the correction remains auditable. Historical dry-run rows that
-- were rolled back will not be present and therefore need no repair.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT artifact_type,artifact_id
        FROM aios.semantic_retention_state
        WHERE state='QUARANTINED'
          AND reason_code='extraction_debris:missing_semantic_predicate'
          AND artifact_type IN ('knowledge_acquisition_event','claim_candidate')
        ORDER BY artifact_type,artifact_id
    LOOP
        PERFORM aios.set_semantic_retention_state(
            r.artifact_type,
            r.artifact_id,
            'ACTIVE',
            'repairable_incomplete_semantic_extraction',
            NULL,NULL,NULL,1.0,
            jsonb_build_object(
                'prior_reason','extraction_debris:missing_semantic_predicate',
                'policy_fix','20260912_semantic_retention_repairable_incomplete'
            )
        );
    END LOOP;
END;
$$;

COMMIT;
