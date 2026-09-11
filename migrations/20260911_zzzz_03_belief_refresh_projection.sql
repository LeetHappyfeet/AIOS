-- AIOS character belief reconciliation phase 3.
BEGIN;

CREATE OR REPLACE FUNCTION aios.refresh_belief_from_character_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_instance_id uuid;
    v_proposition_id uuid;
    v_atom_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        v_instance_id := OLD.instance_id;
        v_proposition_id := OLD.proposition_id;
    ELSE
        v_instance_id := NEW.instance_id;
        v_proposition_id := NEW.proposition_id;
    END IF;

    SELECT atom_id INTO v_atom_id
    FROM aios.proposition
    WHERE proposition_id=v_proposition_id;

    IF v_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_belief_descendants(v_instance_id, v_atom_id);
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_belief_from_character_evidence
ON aios.character_proposition_knowledge;
CREATE TRIGGER trg_refresh_belief_from_character_evidence
AFTER INSERT OR UPDATE OR DELETE
ON aios.character_proposition_knowledge
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_belief_from_character_evidence();

CREATE OR REPLACE FUNCTION aios.refresh_belief_from_admission()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_acquisition_id uuid;
    v_instance_id uuid;
    v_atom_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        v_acquisition_id := OLD.acquisition_id;
    ELSE
        v_acquisition_id := NEW.acquisition_id;
    END IF;

    SELECT kae.instance_id, p.atom_id
    INTO v_instance_id, v_atom_id
    FROM aios.knowledge_acquisition_event kae
    JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
    WHERE kae.acquisition_id=v_acquisition_id;

    IF v_instance_id IS NOT NULL AND v_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_belief_descendants(v_instance_id, v_atom_id);
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_belief_from_admission
ON aios.semantic_evidence_admission;
DROP TRIGGER IF EXISTS trg_refresh_belief_from_admission_update
ON aios.semantic_evidence_admission;
CREATE TRIGGER trg_refresh_belief_from_admission
AFTER INSERT OR DELETE
ON aios.semantic_evidence_admission
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_belief_from_admission();
CREATE TRIGGER trg_refresh_belief_from_admission_update
AFTER UPDATE OF status, confidence
ON aios.semantic_evidence_admission
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_belief_from_admission();

CREATE OR REPLACE FUNCTION aios.refresh_belief_from_ingest_supersession()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
BEGIN
    IF OLD.superseded_at IS NOT DISTINCT FROM NEW.superseded_at THEN
        RETURN NEW;
    END IF;

    FOR rec IN
        SELECT DISTINCT kae.instance_id, p.atom_id
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
        JOIN aios.dag_node dn ON dn.node_id=kae.dag_node_id
        WHERE dn.event_id=NEW.event_id
          AND p.atom_id IS NOT NULL
    LOOP
        PERFORM aios.reconcile_belief_descendants(rec.instance_id, rec.atom_id);
    END LOOP;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_belief_from_ingest_supersession
ON aios.ingest_event;
CREATE TRIGGER trg_refresh_belief_from_ingest_supersession
AFTER UPDATE OF superseded_at
ON aios.ingest_event
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_belief_from_ingest_supersession();

CREATE OR REPLACE FUNCTION aios.refresh_belief_from_acquisition_topology()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_instance_id uuid;
    v_atom_id uuid;
BEGIN
    SELECT kae.instance_id, p.atom_id
    INTO v_instance_id, v_atom_id
    FROM aios.knowledge_acquisition_event kae
    JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
    WHERE kae.acquisition_id=NEW.acquisition_id;

    IF v_instance_id IS NOT NULL AND v_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_belief_descendants(v_instance_id, v_atom_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_belief_from_acquisition_topology
ON aios.semantic_topology_node;
CREATE TRIGGER trg_refresh_belief_from_acquisition_topology
AFTER INSERT OR UPDATE OF acquisition_id
ON aios.semantic_topology_node
FOR EACH ROW
WHEN (NEW.node_type='EPISTEMIC_TRANSITION' AND NEW.acquisition_id IS NOT NULL)
EXECUTE FUNCTION aios.refresh_belief_from_acquisition_topology();

-- ---------------------------------------------------------------------------
-- HUD-compatible active belief projection.
--
-- The underlying character_proposition_knowledge table remains evidence. This
-- view exposes only the proposition currently selected by the reconciled state
-- for each target instance/atom, while retaining the evidence instance that
-- actually supplied the representative proposition.
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
WHERE bs.preferred_proposition_id IS NOT NULL;

COMMENT ON VIEW aios.character_active_proposition_knowledge IS
'Current reconciled /char belief projection. Evidence ownership remains in character_proposition_knowledge.';

COMMIT;
