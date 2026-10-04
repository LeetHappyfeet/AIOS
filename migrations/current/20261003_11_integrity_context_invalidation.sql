-- Complete source-integrity invalidation for edits outside the original
-- claim_candidate.raw_text and frame-resolved-* trigger lists.
-- This is additive: preserve original source/evidence; recompute effective
-- admission and let existing admission invalidation queue descendant beliefs.
BEGIN;

CREATE OR REPLACE FUNCTION aios.refresh_admission_from_section_context_edit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rec record;
BEGIN
  IF OLD.content IS NOT DISTINCT FROM NEW.content THEN RETURN NEW; END IF;
  FOR rec IN
    SELECT DISTINCT kae.acquisition_id
    FROM aios.extracted_sentence es
    JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
    JOIN aios.knowledge_acquisition_event kae ON kae.claim_id=cc.claim_id
    WHERE es.section_id=NEW.section_id
  LOOP
    PERFORM aios.recompute_semantic_evidence_admission(rec.acquisition_id);
  END LOOP;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_refresh_admission_integrity_section_edit
 ON aios.document_section;
CREATE TRIGGER trg_refresh_admission_integrity_section_edit
AFTER UPDATE OF content ON aios.document_section
FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_section_context_edit();

CREATE OR REPLACE FUNCTION aios.refresh_admission_from_integrity_frame_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_claim uuid;
DECLARE rec record;
BEGIN
  v_claim := CASE WHEN TG_OP='DELETE' THEN OLD.claim_id ELSE NEW.claim_id END;
  FOR rec IN SELECT acquisition_id
             FROM aios.knowledge_acquisition_event
             WHERE claim_id=v_claim
  LOOP
    PERFORM aios.recompute_semantic_evidence_admission(rec.acquisition_id);
  END LOOP;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_refresh_admission_integrity_frame_mutation
 ON aios.claim_semantic_frame;
CREATE TRIGGER trg_refresh_admission_integrity_frame_mutation
AFTER INSERT OR DELETE OR UPDATE OF
   frame_index, subject_text, object_text, predicate_surface,
   polarity, modality, decomposer_version
ON aios.claim_semantic_frame
FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_integrity_frame_mutation();

COMMENT ON FUNCTION aios.semantic_integrity_claim_current(uuid) IS
 'Shared read gate for the currently represented claim and V2 semantic-frame snapshot. V4 receipts are also section-digest bound. Older V3 receipts with NULL section digest remain legacy-eligible if their stored frame identity is still current; replay must revalidate them under V4.';

COMMIT;
