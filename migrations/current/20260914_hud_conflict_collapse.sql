-- Collapse false semantic conflict cliques before they can leak into HUD retrieval.
--
-- The HUD already reads current reconciled character state through
-- character_active_proposition_knowledge. The remaining leak is the legacy
-- proposition_conflict table: older broad exclusive-object rows can still be
-- rendered even when the v4 relation verifier rejects them.
--
-- This migration makes proposition_conflict obey the same narrow contradiction
-- semantics as semantic_index/relation_validator.py and cleans historical rows.

BEGIN;

CREATE OR REPLACE FUNCTION aios.validate_proposition_conflict_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_atom_a uuid;
    v_atom_b uuid;
    v_subject_a text;
    v_subject_b text;
    v_predicate_a text;
    v_predicate_b text;
    v_object_a text;
    v_object_b text;
    v_polarity_a integer;
    v_polarity_b integer;
BEGIN
    SELECT atom_id, subject_norm, predicate_norm, object_norm, polarity
    INTO v_atom_a, v_subject_a, v_predicate_a, v_object_a, v_polarity_a
    FROM aios.proposition
    WHERE proposition_id=NEW.proposition_a_id;

    SELECT atom_id, subject_norm, predicate_norm, object_norm, polarity
    INTO v_atom_b, v_subject_b, v_predicate_b, v_object_b, v_polarity_b
    FROM aios.proposition
    WHERE proposition_id=NEW.proposition_b_id;

    -- Opposite polarity is only contradictory when both propositions answer
    -- the exact same polarity-independent semantic atom.
    IF NEW.conflict_type='opposite_polarity' THEN
        IF v_atom_a IS DISTINCT FROM v_atom_b THEN
            RETURN NULL;
        END IF;
        RETURN NEW;
    END IF;

    -- Different positive objects are contradictory only for deliberately
    -- single-valued slots. Generic copular/descriptive predicates such as
    -- "be" and "be_definition_of" are intentionally excluded: "Mia is new",
    -- "Mia is vulnerable", and "Mia is delicate" may all be true together.
    IF NEW.conflict_type='exclusive_object' THEN
        IF v_subject_a IS DISTINCT FROM v_subject_b
           OR v_predicate_a IS DISTINCT FROM v_predicate_b
           OR v_polarity_a IS DISTINCT FROM v_polarity_b
           OR v_object_a IS NULL
           OR v_object_b IS NULL
           OR v_object_a IS NOT DISTINCT FROM v_object_b
           OR lower(COALESCE(v_predicate_a,'')) NOT IN (
                'identity',
                'located_at',
                'location',
                'born_in',
                'status'
           ) THEN
            RETURN NULL;
        END IF;
        RETURN NEW;
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

-- Remove historical conflict rows that the current verifier would never allow
-- to become contradictions. Provenance propositions remain untouched.
DELETE FROM aios.proposition_conflict pc
USING aios.proposition pa, aios.proposition pb
WHERE pc.proposition_a_id=pa.proposition_id
  AND pc.proposition_b_id=pb.proposition_id
  AND (
      (
          pc.conflict_type='opposite_polarity'
          AND pa.atom_id IS DISTINCT FROM pb.atom_id
      )
      OR (
          pc.conflict_type='exclusive_object'
          AND (
              pa.subject_norm IS DISTINCT FROM pb.subject_norm
              OR pa.predicate_norm IS DISTINCT FROM pb.predicate_norm
              OR pa.polarity IS DISTINCT FROM pb.polarity
              OR pa.object_norm IS NULL
              OR pb.object_norm IS NULL
              OR pa.object_norm IS NOT DISTINCT FROM pb.object_norm
              OR lower(COALESCE(pa.predicate_norm,'')) NOT IN (
                    'identity',
                    'located_at',
                    'location',
                    'born_in',
                    'status'
              )
          )
      )
  );

-- Diagnostic view: conflicts that are both structurally valid and independently
-- verified by the current semantic relation classifier. This is the intended
-- authority for future HUD/debug surfaces; proposition_conflict remains a
-- candidate/audit table rather than semantic proof.
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
FROM aios.semantic_neighbor_relation r
WHERE r.relation='CONTRADICTS'
  AND r.status IN ('candidate','reconciled')
  AND COALESCE((r.features->>'character_conflict_eligible')::boolean, false)
ORDER BY
    LEAST(r.proposition_id::text, r.neighbor_proposition_id::text),
    GREATEST(r.proposition_id::text, r.neighbor_proposition_id::text),
    r.confidence DESC,
    r.updated_at DESC;

COMMENT ON VIEW aios.verified_proposition_conflict IS
'Character-same-viewpoint contradictions independently verified by the semantic relation matrix. Generic similarity and legacy proposition_conflict rows are not proof.';

COMMIT;
