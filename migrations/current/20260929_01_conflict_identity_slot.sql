-- Identity/type descriptions are not a generic exclusive slot.
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


DELETE FROM aios.proposition_conflict pc
USING aios.proposition p
WHERE p.proposition_id=pc.proposition_a_id
  AND pc.conflict_type='exclusive_object'
  AND p.predicate_norm='identity';
-- Existing relation receipts must not expose old identity/type slot conflicts.
CREATE OR REPLACE VIEW aios.verified_proposition_conflict AS
SELECT DISTINCT ON (
    LEAST(r.proposition_id::text, r.neighbor_proposition_id::text),
    GREATEST(r.proposition_id::text, r.neighbor_proposition_id::text)
)
    r.proposition_id AS proposition_a_id,
    r.neighbor_proposition_id AS proposition_b_id,
    r.confidence AS strength,
    COALESCE(r.features->>'epistemic_interpretation', 'semantic_conflict_unscoped') AS conflict_type,
    r.features, r.classifier_version, r.updated_at
FROM aios.validated_semantic_neighbor_relation r
JOIN aios.proposition p ON p.proposition_id=r.proposition_id
WHERE r.relation='CONTRADICTS'
  AND r.status IN ('candidate','reconciled')
  AND COALESCE((r.features->>'character_conflict_eligible')::boolean, false)
  AND NOT (p.predicate_norm='identity' AND r.features->>'exclusive_slot_conflict'='true')
ORDER BY
    LEAST(r.proposition_id::text, r.neighbor_proposition_id::text),
    GREATEST(r.proposition_id::text, r.neighbor_proposition_id::text),
    r.confidence DESC, r.updated_at DESC;
COMMIT;
