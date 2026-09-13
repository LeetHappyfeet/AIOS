-- AIOS semantic retention / trash phase 1.
BEGIN;

-- Phase 1 is deliberately non-destructive. It changes only semantic eligibility
-- and audit state. No source/evidence row is physically deleted here.
CREATE TABLE IF NOT EXISTS aios.semantic_retention_state (
    artifact_type text NOT NULL,
    artifact_id text NOT NULL,
    scope_key text,
    state text NOT NULL CHECK (state IN ('ACTIVE','COLD','QUARANTINED','TRASHED')),
    reason_code text NOT NULL,
    utility_score double precision CHECK (utility_score IS NULL OR utility_score BETWEEN 0 AND 1),
    quality_score double precision CHECK (quality_score IS NULL OR quality_score BETWEEN 0 AND 1),
    redundancy_score double precision CHECK (redundancy_score IS NULL OR redundancy_score BETWEEN 0 AND 1),
    recoverability double precision CHECK (recoverability IS NULL OR recoverability BETWEEN 0 AND 1),
    protected boolean NOT NULL DEFAULT false,
    protection_reason text,
    first_flagged_at timestamptz NOT NULL DEFAULT now(),
    last_evaluated_at timestamptz NOT NULL DEFAULT now(),
    trash_after timestamptz,
    resolver_version text NOT NULL DEFAULT 'semantic-retention-v1',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (artifact_type, artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_retention_state_state
    ON aios.semantic_retention_state (state, last_evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_semantic_retention_state_scope
    ON aios.semantic_retention_state (scope_key, state)
    WHERE scope_key IS NOT NULL;

COMMENT ON TABLE aios.semantic_retention_state IS
'Logical retention state only. Absence means ACTIVE. Phase 1 never physically deletes source/evidence artifacts.';

CREATE TABLE IF NOT EXISTS aios.retention_event (
    event_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    artifact_type text NOT NULL,
    artifact_id text NOT NULL,
    old_state text NOT NULL CHECK (old_state IN ('ACTIVE','COLD','QUARANTINED','TRASHED')),
    new_state text NOT NULL CHECK (new_state IN ('ACTIVE','COLD','QUARANTINED','TRASHED')),
    reason_code text NOT NULL,
    score double precision CHECK (score IS NULL OR score BETWEEN 0 AND 1),
    triggered_by text NOT NULL DEFAULT 'semantic-retention-v1',
    related_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    policy_version text NOT NULL DEFAULT 'semantic-retention-v1',
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_retention_event_artifact
    ON aios.retention_event (artifact_type, artifact_id, created_at DESC);

COMMENT ON TABLE aios.retention_event IS
'Append-only audit history for logical retention transitions.';

CREATE OR REPLACE FUNCTION aios.reject_retention_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'aios.retention_event is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_retention_event_append_only ON aios.retention_event;
CREATE TRIGGER trg_retention_event_append_only
BEFORE UPDATE OR DELETE ON aios.retention_event
FOR EACH ROW EXECUTE FUNCTION aios.reject_retention_event_mutation();

-- ---------------------------------------------------------------------------
-- Protection checks.
--
-- Retention never authors /char or /world state. It only removes evidence from
-- consideration, and it refuses to remove the last/selected support currently
-- anchoring a reconciled memory.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION aios.semantic_retention_protection(
    p_artifact_type text,
    p_artifact_id text
)
RETURNS TABLE(protected boolean, reason text, scope_key text)
LANGUAGE plpgsql
AS $$
DECLARE
    v_acquisition_id uuid;
    v_assertion_id uuid;
    v_instance_id uuid;
    v_proposition_id uuid;
    v_atom_id uuid;
    v_world_id uuid;
    v_other_active integer;
BEGIN
    protected := false;
    reason := NULL;
    scope_key := NULL;

    IF p_artifact_type='knowledge_acquisition_event' THEN
        BEGIN
            v_acquisition_id := p_artifact_id::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN NEXT;
            RETURN;
        END;

        SELECT kae.instance_id, kae.proposition_id, p.atom_id,
               'char:' || ci.character_id
        INTO v_instance_id, v_proposition_id, v_atom_id, scope_key
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
        JOIN aios.character_instance ci ON ci.instance_id=kae.instance_id
        WHERE kae.acquisition_id=v_acquisition_id;

        IF v_instance_id IS NULL OR v_atom_id IS NULL THEN
            RETURN NEXT;
            RETURN;
        END IF;

        IF EXISTS (
            SELECT 1
            FROM aios.character_belief_state bs
            WHERE bs.instance_id=v_instance_id
              AND bs.atom_id=v_atom_id
              AND bs.preferred_proposition_id=v_proposition_id
        ) THEN
            SELECT COUNT(*)::integer
            INTO v_other_active
            FROM aios.knowledge_acquisition_event other
            JOIN aios.proposition op ON op.proposition_id=other.proposition_id
            JOIN aios.semantic_evidence_admission sea
              ON sea.acquisition_id=other.acquisition_id
             AND sea.status='active'
            LEFT JOIN aios.semantic_retention_state rs
              ON rs.artifact_type='knowledge_acquisition_event'
             AND rs.artifact_id=other.acquisition_id::text
            WHERE other.instance_id=v_instance_id
              AND op.atom_id=v_atom_id
              AND other.acquisition_id<>v_acquisition_id
              AND COALESCE(rs.state,'ACTIVE')='ACTIVE';

            IF COALESCE(v_other_active,0)=0 THEN
                protected := true;
                reason := 'sole_current_char_support';
            END IF;
        END IF;

        RETURN NEXT;
        RETURN;
    END IF;

    IF p_artifact_type='world_proposition_assertion' THEN
        BEGIN
            v_assertion_id := p_artifact_id::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN NEXT;
            RETURN;
        END;

        SELECT a.world_id, p.atom_id, 'world:' || a.world_id::text
        INTO v_world_id, v_atom_id, scope_key
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        WHERE a.assertion_id=v_assertion_id;

        IF EXISTS (
            SELECT 1
            FROM aios.world_memory_state wms
            WHERE wms.world_id=v_world_id
              AND wms.atom_id=v_atom_id
              AND wms.preferred_assertion_id=v_assertion_id
        ) THEN
            protected := true;
            reason := 'preferred_current_world_support';
        END IF;

        RETURN NEXT;
        RETURN;
    END IF;

    RETURN NEXT;
END;
$$;

-- ---------------------------------------------------------------------------
-- Retention transition API.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION aios.set_semantic_retention_state(
    p_artifact_type text,
    p_artifact_id text,
    p_new_state text,
    p_reason_code text,
    p_utility_score double precision DEFAULT NULL,
    p_quality_score double precision DEFAULT NULL,
    p_redundancy_score double precision DEFAULT NULL,
    p_recoverability double precision DEFAULT NULL,
    p_meta jsonb DEFAULT '{}'::jsonb
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    v_old_state text;
    v_protected boolean;
    v_protection_reason text;
    v_scope_key text;
    v_related_node_id uuid;
    v_acquisition_id uuid;
    v_assertion_id uuid;
    v_atom_id uuid;
    v_world_id uuid;
    v_score double precision;
BEGIN
    IF p_new_state NOT IN ('ACTIVE','COLD','QUARANTINED','TRASHED') THEN
        RAISE EXCEPTION 'invalid retention state: %', p_new_state;
    END IF;

    SELECT state INTO v_old_state
    FROM aios.semantic_retention_state
    WHERE artifact_type=p_artifact_type AND artifact_id=p_artifact_id;
    v_old_state := COALESCE(v_old_state, 'ACTIVE');

    SELECT p.protected, p.reason, p.scope_key
    INTO v_protected, v_protection_reason, v_scope_key
    FROM aios.semantic_retention_protection(p_artifact_type,p_artifact_id) p;

    IF COALESCE(v_protected,false) AND p_new_state<>'ACTIVE' THEN
        INSERT INTO aios.semantic_retention_state (
            artifact_type, artifact_id, scope_key, state, reason_code,
            utility_score, quality_score, redundancy_score, recoverability,
            protected, protection_reason, resolver_version, meta,
            first_flagged_at, last_evaluated_at
        )
        VALUES (
            p_artifact_type,p_artifact_id,v_scope_key,v_old_state,
            'protection_blocked:' || p_reason_code,
            p_utility_score,p_quality_score,p_redundancy_score,p_recoverability,
            true,v_protection_reason,'semantic-retention-v1',
            COALESCE(p_meta,'{}'::jsonb) || jsonb_build_object('requested_state',p_new_state),
            now(),now()
        )
        ON CONFLICT (artifact_type,artifact_id) DO UPDATE
        SET scope_key=EXCLUDED.scope_key,
            reason_code=EXCLUDED.reason_code,
            utility_score=EXCLUDED.utility_score,
            quality_score=EXCLUDED.quality_score,
            redundancy_score=EXCLUDED.redundancy_score,
            recoverability=EXCLUDED.recoverability,
            protected=true,
            protection_reason=EXCLUDED.protection_reason,
            meta=aios.semantic_retention_state.meta || EXCLUDED.meta,
            last_evaluated_at=now();

        INSERT INTO aios.retention_event (
            artifact_type,artifact_id,old_state,new_state,reason_code,score,
            triggered_by,policy_version,meta
        ) VALUES (
            p_artifact_type,p_artifact_id,v_old_state,v_old_state,
            'protection_blocked:' || p_reason_code,
            GREATEST(COALESCE(p_redundancy_score,0),1.0-COALESCE(p_quality_score,1.0)),
            'semantic-retention-v1','semantic-retention-v1',
            COALESCE(p_meta,'{}'::jsonb) || jsonb_build_object(
                'requested_state',p_new_state,
                'protection_reason',v_protection_reason
            )
        );
        RETURN false;
    END IF;

    IF v_old_state=p_new_state THEN
        UPDATE aios.semantic_retention_state
        SET reason_code=p_reason_code,
            utility_score=COALESCE(p_utility_score,utility_score),
            quality_score=COALESCE(p_quality_score,quality_score),
            redundancy_score=COALESCE(p_redundancy_score,redundancy_score),
            recoverability=COALESCE(p_recoverability,recoverability),
            protected=COALESCE(v_protected,false),
            protection_reason=v_protection_reason,
            meta=meta || COALESCE(p_meta,'{}'::jsonb),
            last_evaluated_at=now()
        WHERE artifact_type=p_artifact_type AND artifact_id=p_artifact_id;
        RETURN false;
    END IF;

    INSERT INTO aios.semantic_retention_state (
        artifact_type,artifact_id,scope_key,state,reason_code,
        utility_score,quality_score,redundancy_score,recoverability,
        protected,protection_reason,resolver_version,meta,
        first_flagged_at,last_evaluated_at
    ) VALUES (
        p_artifact_type,p_artifact_id,v_scope_key,p_new_state,p_reason_code,
        p_utility_score,p_quality_score,p_redundancy_score,p_recoverability,
        false,NULL,'semantic-retention-v1',COALESCE(p_meta,'{}'::jsonb),now(),now()
    )
    ON CONFLICT (artifact_type,artifact_id) DO UPDATE
    SET scope_key=EXCLUDED.scope_key,
        state=EXCLUDED.state,
        reason_code=EXCLUDED.reason_code,
        utility_score=COALESCE(EXCLUDED.utility_score,aios.semantic_retention_state.utility_score),
        quality_score=COALESCE(EXCLUDED.quality_score,aios.semantic_retention_state.quality_score),
        redundancy_score=COALESCE(EXCLUDED.redundancy_score,aios.semantic_retention_state.redundancy_score),
        recoverability=COALESCE(EXCLUDED.recoverability,aios.semantic_retention_state.recoverability),
        protected=false,
        protection_reason=NULL,
        resolver_version='semantic-retention-v1',
        meta=aios.semantic_retention_state.meta || EXCLUDED.meta,
        last_evaluated_at=now();

    IF p_artifact_type='knowledge_acquisition_event' THEN
        BEGIN
            v_acquisition_id := p_artifact_id::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            v_acquisition_id := NULL;
        END;
        IF v_acquisition_id IS NOT NULL THEN
            SELECT dag_node_id INTO v_related_node_id
            FROM aios.knowledge_acquisition_event
            WHERE acquisition_id=v_acquisition_id;

            IF p_new_state='ACTIVE' THEN
                PERFORM aios.recompute_semantic_evidence_admission(v_acquisition_id);
            ELSE
                UPDATE aios.semantic_evidence_admission
                SET status='suppressed',
                    reason='retention_' || lower(p_new_state),
                    resolver_version='semantic-retention-v1',
                    meta=meta || jsonb_build_object(
                        'retention_state',p_new_state,
                        'retention_reason',p_reason_code
                    ),
                    resolved_at=now(),
                    updated_at=now()
                WHERE acquisition_id=v_acquisition_id;
            END IF;
        END IF;
    ELSIF p_artifact_type='world_proposition_assertion' THEN
        BEGIN
            v_assertion_id := p_artifact_id::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            v_assertion_id := NULL;
        END;
        IF v_assertion_id IS NOT NULL THEN
            SELECT a.world_id,p.atom_id,a.generated_at_node_id
            INTO v_world_id,v_atom_id,v_related_node_id
            FROM aios.world_proposition_assertion a
            JOIN aios.proposition p ON p.proposition_id=a.proposition_id
            WHERE a.assertion_id=v_assertion_id;
            IF v_world_id IS NOT NULL AND v_atom_id IS NOT NULL THEN
                PERFORM aios.reconcile_world_memory_atom(v_world_id,v_atom_id);
            END IF;
        END IF;
    END IF;

    v_score := GREATEST(
        COALESCE(p_redundancy_score,0),
        1.0-COALESCE(p_quality_score,1.0)
    );
    INSERT INTO aios.retention_event (
        artifact_type,artifact_id,old_state,new_state,reason_code,score,
        triggered_by,related_node_id,policy_version,meta
    ) VALUES (
        p_artifact_type,p_artifact_id,v_old_state,p_new_state,p_reason_code,v_score,
        'semantic-retention-v1',v_related_node_id,'semantic-retention-v1',
        COALESCE(p_meta,'{}'::jsonb)
    );

    RETURN true;
END;
$$;

-- ---------------------------------------------------------------------------
-- Automatic Phase-1 evaluator for /char evidence.
--
-- Only deterministic extraction debris and exact same-event duplicates are
-- automatically quarantined. Ambiguity/unresolved semantics remain unresolved,
-- not trash, unless they match one of the explicit structural-debris reasons.
-- ---------------------------------------------------------------------------
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

    IF v_reason IN (
        'missing_semantic_predicate',
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
          SELECT created_at FROM aios.knowledge_acquisition_event
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

CREATE OR REPLACE FUNCTION aios.auto_evaluate_acquisition_retention()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM aios.evaluate_acquisition_retention(NEW.acquisition_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_zz_auto_evaluate_acquisition_retention
ON aios.knowledge_acquisition_event;
CREATE TRIGGER trg_zz_auto_evaluate_acquisition_retention
AFTER INSERT OR UPDATE OF proposition_id,claim_id,instance_id,dag_node_id
ON aios.knowledge_acquisition_event
FOR EACH ROW
EXECUTE FUNCTION aios.auto_evaluate_acquisition_retention();

-- ---------------------------------------------------------------------------
-- Make /world reconciliation retention-aware without changing truth status.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION aios.reconcile_world_memory_atom(
    p_world_id uuid,
    p_atom_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_positive_support double precision := 0.0;
    v_negative_support double precision := 0.0;
    v_evidence_count integer := 0;
    v_independent_count integer := 0;
    v_stance text;
    v_state_confidence double precision := 0.0;
    v_preferred_proposition_id uuid;
    v_preferred_assertion_id uuid;
    v_resolved_through_node_id uuid;
BEGIN
    WITH evidence AS (
        SELECT a.assertion_id,a.proposition_id,p.polarity,
               LEAST(0.999999,GREATEST(0.0,COALESCE(a.confidence,0.0))) AS weight,
               COALESCE(a.source_kind,'unknown') || ':' || a.assertion_id::text AS correlation_key,
               a.generated_at_node_id,a.updated_at
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        LEFT JOIN aios.semantic_retention_state rs
          ON rs.artifact_type='world_proposition_assertion'
         AND rs.artifact_id=a.assertion_id::text
        WHERE a.world_id=p_world_id
          AND p.atom_id=p_atom_id
          AND a.epistemic_status NOT IN ('rejected','superseded')
          AND COALESCE(rs.state,'ACTIVE')='ACTIVE'
    ), correlated AS (
        SELECT polarity,correlation_key,MAX(weight) AS weight
        FROM evidence GROUP BY polarity,correlation_key
    )
    SELECT
        COALESCE(1.0-exp(SUM(ln(1.0-weight)) FILTER (WHERE polarity=1)),0.0),
        COALESCE(1.0-exp(SUM(ln(1.0-weight)) FILTER (WHERE polarity=-1)),0.0),
        (SELECT COUNT(*) FROM evidence),COUNT(*)
    INTO v_positive_support,v_negative_support,v_evidence_count,v_independent_count
    FROM correlated;

    IF v_evidence_count=0 THEN
        DELETE FROM aios.world_memory_state
        WHERE world_id=p_world_id AND atom_id=p_atom_id;
        RETURN;
    END IF;

    IF v_positive_support>=0.60 AND (v_positive_support-v_negative_support)>=0.15 THEN
        v_stance:='positive';
    ELSIF v_negative_support>=0.60 AND (v_negative_support-v_positive_support)>=0.15 THEN
        v_stance:='negative';
    ELSE
        v_stance:='unresolved';
    END IF;
    v_state_confidence:=LEAST(1.0,GREATEST(0.0,abs(v_positive_support-v_negative_support)));

    SELECT a.proposition_id,a.assertion_id,a.generated_at_node_id
    INTO v_preferred_proposition_id,v_preferred_assertion_id,v_resolved_through_node_id
    FROM aios.world_proposition_assertion a
    JOIN aios.proposition p ON p.proposition_id=a.proposition_id
    LEFT JOIN aios.semantic_retention_state rs
      ON rs.artifact_type='world_proposition_assertion'
     AND rs.artifact_id=a.assertion_id::text
    WHERE a.world_id=p_world_id
      AND p.atom_id=p_atom_id
      AND a.epistemic_status NOT IN ('rejected','superseded')
      AND COALESCE(rs.state,'ACTIVE')='ACTIVE'
    ORDER BY
        CASE
            WHEN v_stance='positive' AND p.polarity=1 THEN 0
            WHEN v_stance='negative' AND p.polarity=-1 THEN 0
            WHEN v_stance='unresolved' THEN 0 ELSE 1
        END,
        a.confidence DESC,a.updated_at DESC,a.assertion_id
    LIMIT 1;

    INSERT INTO aios.world_memory_state (
        world_id,atom_id,stance,positive_support,negative_support,state_confidence,
        preferred_proposition_id,preferred_assertion_id,evidence_count,
        independent_evidence_count,resolved_through_node_id,resolver_version,meta,
        resolved_at,updated_at
    ) VALUES (
        p_world_id,p_atom_id,v_stance,v_positive_support,v_negative_support,
        v_state_confidence,v_preferred_proposition_id,v_preferred_assertion_id,
        v_evidence_count,v_independent_count,v_resolved_through_node_id,
        'memory-reconciliation-v1',
        jsonb_build_object(
            'path','world','authority_boundary','world_proposition_assertion',
            'evidence_topology_preserved',true,'retention_filter','semantic-retention-v1',
            'accept_support',0.60,'decision_margin',0.15
        ),now(),now()
    )
    ON CONFLICT (world_id,atom_id) DO UPDATE
    SET stance=EXCLUDED.stance,
        positive_support=EXCLUDED.positive_support,
        negative_support=EXCLUDED.negative_support,
        state_confidence=EXCLUDED.state_confidence,
        preferred_proposition_id=EXCLUDED.preferred_proposition_id,
        preferred_assertion_id=EXCLUDED.preferred_assertion_id,
        evidence_count=EXCLUDED.evidence_count,
        independent_evidence_count=EXCLUDED.independent_evidence_count,
        resolved_through_node_id=EXCLUDED.resolved_through_node_id,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,resolved_at=now(),updated_at=now();
END;
$$;

COMMIT;
