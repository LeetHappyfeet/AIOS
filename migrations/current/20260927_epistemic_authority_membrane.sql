-- Epistemic authority membrane for persistent character cognition.
-- 2026-09-27
--
-- Semantic admission answers whether evidence is usable. This migration adds
-- the orthogonal question: what is that evidence allowed to establish?
-- Origin and lineage are immutable; authority can be recomputed without
-- rewriting history.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.epistemic_authority_admission (
    acquisition_id uuid PRIMARY KEY
        REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE,
    origin_kind text NOT NULL,
    epistemic_mode text NOT NULL,
    authority_state text NOT NULL
        CHECK (authority_state IN ('candidate','attested','corroborated','canonical','disputed','retracted')),
    authority_rank smallint NOT NULL CHECK (authority_rank BETWEEN 0 AND 4),
    lineage_key text NOT NULL,
    predicate_class text NOT NULL DEFAULT 'general',
    authorized_uses text[] NOT NULL DEFAULT ARRAY['belief','reflection']::text[],
    decision_reason text NOT NULL,
    policy_version text NOT NULL DEFAULT 'epistemic-authority-v1',
    decided_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_epistemic_authority_lineage
    ON aios.epistemic_authority_admission (lineage_key, authority_rank);
CREATE INDEX IF NOT EXISTS idx_epistemic_authority_origin
    ON aios.epistemic_authority_admission (origin_kind, epistemic_mode, authority_state);

COMMENT ON TABLE aios.epistemic_authority_admission IS
'Orthogonal authority membrane for /char evidence. Immutable origin/lineage prevents generated cognition from becoming independent corroboration or objective history merely through persistence/retrieval.';

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
        -- A character-authored SillyTavern turn is generated character cognition,
        -- even when its surface form is a memory or narrated observation.
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

CREATE OR REPLACE FUNCTION aios.trg_classify_epistemic_authority()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM aios.classify_epistemic_authority(NEW.acquisition_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_classify_epistemic_authority ON aios.knowledge_acquisition_event;
CREATE TRIGGER trg_classify_epistemic_authority
AFTER INSERT OR UPDATE OF proposition_id, claim_id, epistemic_status, confidence, meta
ON aios.knowledge_acquisition_event
FOR EACH ROW EXECUTE FUNCTION aios.trg_classify_epistemic_authority();

-- Existing evidence is admitted conservatively. Nothing is deleted or silently
-- promoted; model-authored evidence remains available to subjective cognition.
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT acquisition_id FROM aios.knowledge_acquisition_event LOOP
    PERFORM aios.classify_epistemic_authority(r.acquisition_id);
  END LOOP;
END $$;

COMMIT;
