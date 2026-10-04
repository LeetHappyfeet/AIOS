-- Eligibility completion invalidations. An acquisition can arrive before
-- observation->frame binding and before standalone interpretation. Because
-- admission now checks the exact source occurrence, those later materializations
-- must actively revisit earlier unresolved decisions. No claim/evidence deletion.
BEGIN;

CREATE OR REPLACE FUNCTION aios.revisit_admission_after_occurrence_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_observation uuid;
DECLARE v_claim uuid;
DECLARE rec record;
BEGIN
 v_observation := CASE WHEN TG_OP='DELETE' THEN OLD.observation_id
                       ELSE NEW.observation_id END;
 SELECT claim_id INTO v_claim
 FROM aios.observation WHERE observation_id=v_observation;
 IF v_claim IS NULL THEN
   RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
 END IF;
 FOR rec IN SELECT acquisition_id FROM aios.knowledge_acquisition_event
            WHERE claim_id=v_claim
 LOOP
   PERFORM aios.recompute_semantic_evidence_admission(rec.acquisition_id);
 END LOOP;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_revisit_admission_after_occurrence_binding
 ON aios.observation_proposition;
CREATE TRIGGER trg_revisit_admission_after_occurrence_binding
AFTER INSERT OR DELETE OR UPDATE OF frame_id,proposition_id,observation_id
ON aios.observation_proposition
FOR EACH ROW EXECUTE FUNCTION aios.revisit_admission_after_occurrence_binding();

CREATE OR REPLACE FUNCTION aios.revisit_admission_after_interpretation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_claim uuid;
DECLARE rec record;
BEGIN
 v_claim := CASE WHEN TG_OP='DELETE' THEN OLD.claim_id ELSE NEW.claim_id END;
 FOR rec IN SELECT acquisition_id FROM aios.knowledge_acquisition_event
            WHERE claim_id=v_claim
 LOOP
   PERFORM aios.recompute_semantic_evidence_admission(rec.acquisition_id);
 END LOOP;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_revisit_admission_after_interpretation
 ON aios.semantic_interpretation;
CREATE TRIGGER trg_revisit_admission_after_interpretation
AFTER INSERT OR DELETE OR UPDATE OF standalone_semantic,frame_id,claim_id
ON aios.semantic_interpretation
FOR EACH ROW EXECUTE FUNCTION aios.revisit_admission_after_interpretation();
COMMIT;
