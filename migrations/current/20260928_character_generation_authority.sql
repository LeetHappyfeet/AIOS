-- Forward-only authority classifier update.
-- Do not edit 20260927_epistemic_authority_membrane.sql after it has been applied.
--
-- Existing admissions deliberately retain immutable origin_kind and lineage_key.
-- This classifier applies the generated-character rule to new acquisitions while
-- preserving the original membrane's immutability contract.

CREATE OR REPLACE FUNCTION aios.classify_epistemic_authority(p_acquisition_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    r record;
    v_origin text;
    v_mode text;
    v_state text;
    v_rank smallint;
    v_lineage text;
    v_uses text[];
    v_reason text;
BEGIN
    SELECT kae.*, cc.raw_text, ccr.claim_kind, sf.discourse_mode,
           ie.source, ie.source_kind, ie.source_event_id, ie.message_text,
           ie.payload, ie.speaker_role, ie.character_id
    INTO r
    FROM aios.knowledge_acquisition_event kae
    LEFT JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
    LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=kae.claim_id
    LEFT JOIN aios.claim_semantic_frame_projection sfp ON sfp.claim_id=kae.claim_id
    LEFT JOIN aios.claim_semantic_frame sf ON sf.frame_id=sfp.primary_frame_id
    LEFT JOIN aios.dag_node dn ON dn.node_id=kae.dag_node_id
    LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
    WHERE kae.acquisition_id=p_acquisition_id;

    IF NOT FOUND THEN RETURN; END IF;

    v_origin := COALESCE(NULLIF(r.meta->>'origin_kind',''), CASE
        WHEN lower(COALESCE(r.acquisition_mode,'')) IN ('read','read_document','research','import') THEN 'corpus_source'
        WHEN lower(COALESCE(r.acquisition_mode,''))='taught' THEN 'testimony'
        WHEN lower(COALESCE(r.source_kind,'')) IN ('sensor','simulator','deterministic_plugin') THEN 'deterministic_tool'
        -- Character-authored SillyTavern turns are generated cognition, not
        -- independent testimony or objective observation.
        WHEN lower(COALESCE(r.source_kind,''))='sillytavern_chat'
             AND lower(COALESCE(r.speaker_role::text,''))='character'
             THEN 'character_generation'
        WHEN lower(COALESCE(r.speaker_role::text,''))='user' THEN 'user_testimony'
        WHEN lower(COALESCE(r.speaker_role::text,''))='assistant' THEN 'model_generation'
        WHEN lower(COALESCE(r.meta->>'source','')) LIKE 'context-resolver%' THEN 'model_inference'
        ELSE 'unknown'
    END);

    v_mode := COALESCE(NULLIF(r.meta->>'epistemic_mode',''), CASE
        WHEN lower(COALESCE(r.discourse_mode,'')) IN ('hypothetical','counterfactual','conditional') THEN 'hypothesis'
        WHEN lower(COALESCE(r.discourse_mode,'')) LIKE '%question%' THEN 'question'
        WHEN upper(COALESCE(r.claim_kind,''))='MEMORY' THEN 'memory'
        WHEN upper(COALESCE(r.claim_kind,''))='BELIEF' THEN 'belief'
        WHEN upper(COALESCE(r.claim_kind,'')) IN ('TRAIT','STATE','RELATIONSHIP') THEN 'self_description'
        WHEN v_origin IN ('model_generation','model_inference') THEN 'inference'
        WHEN v_origin IN ('user_testimony','testimony') THEN 'testimony'
        ELSE 'observation'
    END);

    v_lineage := COALESCE(NULLIF(r.meta->>'origin_lineage_id',''),
        aios.evidence_correlation_key(
            r.source, r.source_kind, r.source_event_id, r.message_text,
            COALESCE(r.payload,'{}'::jsonb), r.speaker_role::text, r.character_id,
            COALESCE(r.meta->>'source_key', r.source_entity_id::text, 'unknown'),
            COALESCE(r.dag_node_id::text, r.acquisition_id::text)));

    IF v_origin='deterministic_tool' THEN
        v_state:='canonical'; v_rank:=4;
        v_uses:=ARRAY['belief','reflection','planning','action_precondition'];
        v_reason:='typed_deterministic_source';
    ELSIF v_origin IN ('user_testimony','testimony','corpus_source') THEN
        v_state:='attested'; v_rank:=2;
        v_uses:=ARRAY['belief','reflection','planning'];
        v_reason:='external_attested_evidence';
    ELSIF v_origin IN ('model_generation','model_inference','character_generation') THEN
        v_state:='candidate'; v_rank:=0;
        v_uses:=ARRAY['belief','reflection'];
        v_reason:='generated_cognition_has_no_historical_authority';
    ELSE
        v_state:='candidate'; v_rank:=1;
        v_uses:=ARRAY['belief','reflection'];
        v_reason:='unknown_origin_is_not_promoted_by_default';
    END IF;

    INSERT INTO aios.epistemic_authority_admission(
        acquisition_id,origin_kind,epistemic_mode,authority_state,authority_rank,
        lineage_key,predicate_class,authorized_uses,decision_reason,meta)
    VALUES(p_acquisition_id,v_origin,v_mode,v_state,v_rank,v_lineage,
           lower(COALESCE(r.claim_kind,'general')),v_uses,v_reason,
           jsonb_build_object('immutable_origin',true))
    ON CONFLICT (acquisition_id) DO UPDATE SET
        -- Origin and lineage deliberately never change after first admission.
        epistemic_mode=EXCLUDED.epistemic_mode,
        authority_state=EXCLUDED.authority_state,
        authority_rank=EXCLUDED.authority_rank,
        predicate_class=EXCLUDED.predicate_class,
        authorized_uses=EXCLUDED.authorized_uses,
        decision_reason=EXCLUDED.decision_reason,
        policy_version=EXCLUDED.policy_version,
        decided_at=now(),
        meta=aios.epistemic_authority_admission.meta || EXCLUDED.meta;
END;
$$;


COMMENT ON FUNCTION aios.classify_epistemic_authority(uuid) IS
'Classifies acquisition authority. SillyTavern character-authored turns are generated character cognition; existing immutable origin/lineage admissions are not rewritten.';
