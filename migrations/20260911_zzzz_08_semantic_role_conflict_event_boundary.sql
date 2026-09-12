-- Tighten semantic role compatibility, conflict identity, and derived EVENT visibility.
BEGIN;

-- ---------------------------------------------------------------------------
-- Conflict identity is polarity-independent semantic atom identity.
-- A topic (subject + predicate) is too broad: "Mia is tall" and
-- "Mia is not a neat freak" are not opposite-polarity conflicts.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION aios.validate_proposition_conflict_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_atom_a uuid;
    v_atom_b uuid;
    v_predicate_a text;
    v_predicate_b text;
BEGIN
    SELECT atom_id, predicate_norm
    INTO v_atom_a, v_predicate_a
    FROM aios.proposition
    WHERE proposition_id=NEW.proposition_a_id;

    SELECT atom_id, predicate_norm
    INTO v_atom_b, v_predicate_b
    FROM aios.proposition
    WHERE proposition_id=NEW.proposition_b_id;

    IF NEW.conflict_type='opposite_polarity'
       AND v_atom_a IS DISTINCT FROM v_atom_b THEN
        RETURN NULL;
    END IF;

    IF NEW.conflict_type='exclusive_object'
       AND (v_predicate_a='be_definition_of' OR v_predicate_b='be_definition_of') THEN
        RETURN NULL;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_reject_nonexclusive_definition_conflict
ON aios.proposition_conflict;
DROP TRIGGER IF EXISTS trg_validate_proposition_conflict_identity
ON aios.proposition_conflict;
CREATE TRIGGER trg_validate_proposition_conflict_identity
BEFORE INSERT OR UPDATE OF conflict_type, proposition_a_id, proposition_b_id
ON aios.proposition_conflict
FOR EACH ROW
EXECUTE FUNCTION aios.validate_proposition_conflict_identity();

DELETE FROM aios.proposition_conflict pc
USING aios.proposition pa, aios.proposition pb
WHERE pc.proposition_a_id=pa.proposition_id
  AND pc.proposition_b_id=pb.proposition_id
  AND (
      (pc.conflict_type='opposite_polarity' AND pa.atom_id IS DISTINCT FROM pb.atom_id)
      OR (
          pc.conflict_type='exclusive_object'
          AND (pa.predicate_norm='be_definition_of' OR pb.predicate_norm='be_definition_of')
      )
  );

-- ---------------------------------------------------------------------------
-- Semantic admission v3.
-- Structural checks from v2 remain.  This adds conservative typed role
-- compatibility.  Unknown roles are not guessed; only known-incompatible
-- tuples are quarantined.  Evidence remains append-only and inspectable.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION aios.recompute_semantic_evidence_admission(
    p_acquisition_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_claim_id uuid;
    v_proposition_id uuid;
    v_raw_text text;
    v_subject_text text;
    v_object_text text;
    v_predicate text;
    v_resolved_subject text;
    v_resolved_object text;
    v_resolution_status text;
    v_discourse_mode text;
    v_epistemic_scope text;
    v_subject_kind text;
    v_object_kind text;
    v_predicate_family text;
    v_claim_kind text;
    v_frame_confidence double precision;
    v_referent_confidence double precision;
    v_context_confidence double precision;
    v_frame_polarity integer;
    v_prop_subject text;
    v_prop_predicate text;
    v_prop_object text;
    v_prop_polarity integer;
    v_status text;
    v_reason text;
    v_confidence double precision;
    v_ambiguous_quoted_participant boolean := false;
    v_internal_frame_reference boolean := false;
    v_serialized_component boolean := false;
    v_negative_scope_unresolved boolean := false;
    v_semantic_self_definition boolean := false;
    v_semantic_role_incompatible boolean := false;
BEGIN
    SELECT kae.claim_id, kae.proposition_id
    INTO v_claim_id, v_proposition_id
    FROM aios.knowledge_acquisition_event kae
    WHERE kae.acquisition_id=p_acquisition_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT p.subject_norm, p.predicate_norm, p.object_norm, p.polarity
    INTO v_prop_subject, v_prop_predicate, v_prop_object, v_prop_polarity
    FROM aios.proposition p
    WHERE p.proposition_id=v_proposition_id;

    IF v_claim_id IS NULL THEN
        v_status := 'active';
        v_reason := 'explicit_nonclaim_acquisition';
        v_confidence := 1.0;
    ELSE
        SELECT
            cc.raw_text,
            sf.subject_text,
            sf.object_text,
            sf.predicate_canonical,
            sf.resolved_subject,
            sf.resolved_object,
            sf.resolution_status,
            sf.discourse_mode,
            sf.frame_confidence,
            sf.referent_confidence,
            sf.polarity,
            ccr.epistemic_scope,
            ccr.subject_kind,
            ccr.object_kind,
            ccr.predicate_family,
            ccr.claim_kind,
            ccr.confidence
        INTO
            v_raw_text,
            v_subject_text,
            v_object_text,
            v_predicate,
            v_resolved_subject,
            v_resolved_object,
            v_resolution_status,
            v_discourse_mode,
            v_frame_confidence,
            v_referent_confidence,
            v_frame_polarity,
            v_epistemic_scope,
            v_subject_kind,
            v_object_kind,
            v_predicate_family,
            v_claim_kind,
            v_context_confidence
        FROM aios.claim_candidate cc
        LEFT JOIN aios.claim_semantic_frame_projection sfp ON sfp.claim_id=cc.claim_id
        LEFT JOIN aios.claim_semantic_frame sf ON sf.frame_id=sfp.primary_frame_id
        LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
        WHERE cc.claim_id=v_claim_id;

        IF NOT FOUND OR v_predicate IS NULL THEN
            v_status := 'suppressed';
            v_reason := 'missing_semantic_predicate';
            v_confidence := 0.0;
        ELSE
            v_internal_frame_reference :=
                COALESCE(v_prop_subject, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+'
                OR COALESCE(v_prop_predicate, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+'
                OR COALESCE(v_prop_object, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+'
                OR COALESCE(v_resolved_subject, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+'
                OR COALESCE(v_resolved_object, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+';

            v_serialized_component :=
                position('|' in COALESCE(v_prop_subject, '')) > 0
                OR position('|' in COALESCE(v_prop_predicate, '')) > 0
                OR position('|' in COALESCE(v_prop_object, '')) > 0
                OR position('|' in COALESCE(v_resolved_subject, '')) > 0
                OR position('|' in COALESCE(v_resolved_object, '')) > 0;

            v_negative_scope_unresolved :=
                COALESCE(v_prop_polarity, 1) = -1
                AND (
                    v_frame_polarity IS DISTINCT FROM -1
                    OR COALESCE(v_resolution_status, 'partial') <> 'resolved'
                );

            v_semantic_self_definition :=
                COALESCE(v_prop_predicate, '')='be_definition_of'
                AND NULLIF(btrim(COALESCE(v_prop_subject, '')), '') IS NOT NULL
                AND lower(btrim(v_prop_subject))=lower(btrim(COALESCE(v_prop_object, '')));

            -- Reject only role combinations the resolver classified strongly
            -- enough to prove incompatible. UNKNOWN remains unresolved by later
            -- evidence rather than being guessed here.
            v_semantic_role_incompatible :=
                CASE COALESCE(v_predicate_family, 'UNKNOWN')
                    WHEN 'SPATIAL' THEN
                        COALESCE(v_object_kind, 'UNKNOWN') NOT IN ('LOCATION','UNKNOWN')
                    WHEN 'TEMPORAL' THEN
                        COALESCE(v_object_kind, 'UNKNOWN') NOT IN ('TIME','UNKNOWN')
                    WHEN 'SOCIAL' THEN
                        COALESCE(v_object_kind, 'UNKNOWN') NOT IN ('PERSON','RELATIONSHIP','UNKNOWN')
                    WHEN 'MEMBERSHIP' THEN
                        COALESCE(v_object_kind, 'UNKNOWN') NOT IN ('ORGANIZATION','UNKNOWN')
                    WHEN 'POSSESSION' THEN
                        COALESCE(v_subject_kind, 'UNKNOWN') IN ('TIME','EVENT','MEMORY','RULE','GOAL')
                    WHEN 'ACTION' THEN
                        COALESCE(v_subject_kind, 'UNKNOWN') IN ('TIME','RULE','GOAL','QUANTITY')
                    WHEN 'IDENTITY' THEN
                        COALESCE(v_prop_predicate, '')='be_definition_of'
                        AND COALESCE(v_object_kind, 'UNKNOWN') IN (
                            'LOCATION','TIME','EVENT','ACTION','QUANTITY','MEMORY','RELATIONSHIP','GOAL','RULE'
                        )
                    ELSE false
                END;

            v_ambiguous_quoted_participant :=
                COALESCE(v_discourse_mode, 'narrated_observation')='narrated_observation'
                AND btrim(COALESCE(v_raw_text, '')) ~ '^["“].*["”][.!?]?$'
                AND (
                    lower(COALESCE(v_subject_text, '')) ~ '(^|[[:space:]])(i|me|my|mine|we|us|our|ours|you|your|yours)([[:space:]]|$)'
                    OR lower(COALESCE(v_object_text, '')) ~ '(^|[[:space:]])(i|me|my|mine|we|us|our|ours|you|your|yours)([[:space:]]|$)'
                );

            IF v_internal_frame_reference THEN
                v_status := 'unresolved';
                v_reason := 'internal_frame_reference';
            ELSIF v_serialized_component THEN
                v_status := 'unresolved';
                v_reason := 'serialized_semantic_component';
            ELSIF v_negative_scope_unresolved THEN
                v_status := 'unresolved';
                v_reason := 'negative_polarity_scope_unresolved';
            ELSIF v_semantic_self_definition THEN
                v_status := 'unresolved';
                v_reason := 'semantic_self_definition';
            ELSIF v_semantic_role_incompatible THEN
                v_status := 'unresolved';
                v_reason := 'semantic_role_incompatible';
            ELSIF v_ambiguous_quoted_participant THEN
                v_status := 'unresolved';
                v_reason := 'quoted_local_discourse_unresolved';
            ELSIF COALESCE(v_resolution_status, 'partial') <> 'resolved' THEN
                v_status := 'unresolved';
                v_reason := 'semantic_frame_partial';
            ELSIF COALESCE(v_referent_confidence, 0.0) < 0.55 THEN
                v_status := 'unresolved';
                v_reason := 'referent_confidence_below_threshold';
            ELSIF COALESCE(v_frame_confidence, 0.0) < 0.55 THEN
                v_status := 'unresolved';
                v_reason := 'frame_confidence_below_threshold';
            ELSE
                v_status := 'active';
                v_reason := 'admitted';
            END IF;

            v_confidence := LEAST(
                1.0,
                GREATEST(
                    0.0,
                    LEAST(
                        COALESCE(v_frame_confidence, 0.0),
                        COALESCE(v_context_confidence, 1.0)
                    )
                )
            );
        END IF;
    END IF;

    INSERT INTO aios.semantic_evidence_admission (
        acquisition_id, status, reason, confidence, resolver_version, meta,
        resolved_at, updated_at
    )
    VALUES (
        p_acquisition_id,
        v_status,
        v_reason,
        v_confidence,
        'semantic-admission-v3',
        jsonb_build_object(
            'claim_id', v_claim_id,
            'proposition_id', v_proposition_id,
            'epistemic_scope', v_epistemic_scope,
            'claim_kind', v_claim_kind,
            'subject_kind', v_subject_kind,
            'object_kind', v_object_kind,
            'predicate_family', v_predicate_family,
            'discourse_mode', v_discourse_mode,
            'ambiguous_quoted_participant', v_ambiguous_quoted_participant,
            'internal_frame_reference', v_internal_frame_reference,
            'serialized_semantic_component', v_serialized_component,
            'negative_polarity_scope_unresolved', v_negative_scope_unresolved,
            'semantic_self_definition', v_semantic_self_definition,
            'semantic_role_incompatible', v_semantic_role_incompatible,
            'frame_polarity', v_frame_polarity,
            'proposition_polarity', v_prop_polarity,
            'structural_firewall', true,
            'typed_role_firewall', true
        ),
        now(),
        now()
    )
    ON CONFLICT (acquisition_id) DO UPDATE
    SET status=EXCLUDED.status,
        reason=EXCLUDED.reason,
        confidence=EXCLUDED.confidence,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,
        resolved_at=now(),
        updated_at=now();
END;
$$;

-- Re-evaluate existing evidence. Admission UPDATE triggers already reconcile
-- affected belief atoms, so this repairs existing /char state without re-ingest.
DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT acquisition_id
        FROM aios.knowledge_acquisition_event
        ORDER BY created_at, acquisition_id
    LOOP
        PERFORM aios.recompute_semantic_evidence_admission(rec.acquisition_id);
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- Active cognition must not present an unresolved or negative EVENT assertion
-- as if it were an event that happened. Raw DAG/source messages remain visible
-- through RECENT EVENTS; only derived event summaries require positive belief.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW aios.character_active_proposition_knowledge AS
SELECT
    bs.instance_id,
    cpk.instance_id AS evidence_instance_id,
    cpk.proposition_id,
    CASE bs.stance
        WHEN 'positive' THEN 'believed'
        WHEN 'negative' THEN 'disbelieved'
        ELSE 'uncertain'
    END AS epistemic_status,
    bs.belief_confidence AS confidence,
    cpk.acquisition_mode,
    cpk.source_entity_id,
    cpk.first_node_id,
    cpk.last_node_id,
    cpk.first_acquired_at,
    bs.resolved_at AS updated_at,
    cpk.meta || jsonb_build_object(
        'atom_id', bs.atom_id,
        'belief_stance', bs.stance,
        'positive_support', bs.positive_support,
        'negative_support', bs.negative_support,
        'belief_resolver_version', bs.resolver_version
    ) AS meta,
    cpk.base_confidence,
    cpk.attention_weight,
    cpk.trust_weight,
    cpk.compatibility_weight,
    cpk.retention_weight,
    cpk.salience_weight,
    bs.belief_confidence AS effective_confidence,
    bs.atom_id,
    bs.stance,
    bs.positive_support,
    bs.negative_support,
    bs.evidence_count,
    bs.independent_evidence_count
FROM aios.character_belief_state bs
JOIN aios.character_proposition_knowledge cpk
  ON cpk.instance_id=bs.preferred_evidence_instance_id
 AND cpk.proposition_id=bs.preferred_proposition_id
WHERE bs.preferred_proposition_id IS NOT NULL
  AND (
      bs.stance='positive'
      OR NOT EXISTS (
          SELECT 1
          FROM aios.observation obs
          JOIN aios.claim_context_resolution ccr
            ON ccr.claim_id=obs.claim_id
          WHERE obs.proposition_id=bs.preferred_proposition_id
            AND ccr.claim_kind='EVENT'
      )
  );

COMMENT ON VIEW aios.character_active_proposition_knowledge IS
'Current reconciled /char belief projection. Evidence ownership remains in character_proposition_knowledge. Derived EVENT cognition is agent-visible only when reconciled positive; raw DAG events remain independently visible.';

COMMIT;