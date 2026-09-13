-- AIOS character belief reconciliation phase 4.
BEGIN;

-- ---------------------------------------------------------------------------
-- Backfill admission and belief state for existing evidence.
-- ---------------------------------------------------------------------------

ALTER TABLE aios.semantic_evidence_admission
    DISABLE TRIGGER trg_refresh_belief_from_admission;
ALTER TABLE aios.semantic_evidence_admission
    DISABLE TRIGGER trg_refresh_belief_from_admission_update;

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

ALTER TABLE aios.semantic_evidence_admission
    ENABLE TRIGGER trg_refresh_belief_from_admission;
ALTER TABLE aios.semantic_evidence_admission
    ENABLE TRIGGER trg_refresh_belief_from_admission_update;

DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        WITH RECURSIVE lineage AS (
            SELECT
                ci.instance_id AS target_instance_id,
                ci.instance_id AS evidence_instance_id,
                ci.parent_instance_id
            FROM aios.character_instance ci

            UNION ALL

            SELECT
                lineage.target_instance_id,
                parent.instance_id,
                parent.parent_instance_id
            FROM lineage
            JOIN aios.character_instance parent
              ON parent.instance_id=lineage.parent_instance_id
        )
        SELECT DISTINCT lineage.target_instance_id, p.atom_id
        FROM lineage
        JOIN aios.character_proposition_knowledge cpk
          ON cpk.instance_id=lineage.evidence_instance_id
        JOIN aios.proposition p
          ON p.proposition_id=cpk.proposition_id
        WHERE p.atom_id IS NOT NULL
        ORDER BY lineage.target_instance_id, p.atom_id
    LOOP
        PERFORM aios.reconcile_character_belief_atom(
            rec.target_instance_id,
            rec.atom_id
        );
    END LOOP;
END;
$$;

COMMIT;
