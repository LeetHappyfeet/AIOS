-- Bound character belief RDF writes to the beliefs that actually changed.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.rdf_character_belief_dirty (
    instance_id uuid NOT NULL,
    atom_id uuid NOT NULL,
    dirty_version bigint NOT NULL DEFAULT 1,
    dirty_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, atom_id)
);

CREATE INDEX IF NOT EXISTS idx_rdf_character_belief_dirty_instance
    ON aios.rdf_character_belief_dirty (instance_id, dirty_at, atom_id);

CREATE TABLE IF NOT EXISTS aios.rdf_character_belief_projection (
    instance_id uuid NOT NULL,
    atom_id uuid NOT NULL,
    projection_hash text NOT NULL,
    projection_version text NOT NULL,
    projected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, atom_id)
);

COMMENT ON TABLE aios.rdf_character_belief_dirty IS
'Coalescing outbox for incremental /char RDF belief projection. One row represents the latest mutation for one semantic atom in one character instance.';

COMMENT ON TABLE aios.rdf_character_belief_projection IS
'Last successfully materialized character-belief RDF fingerprint. Fuseki is a derived view; PostgreSQL remains authoritative.';

CREATE OR REPLACE FUNCTION aios.mark_character_belief_rdf_dirty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_instance_id uuid;
    v_atom_id uuid;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Ignore reconciliation bookkeeping churn when the RDF-visible state is
        -- unchanged. resolved_at/updated_at deliberately are not RDF identity.
        IF ROW(
            OLD.stance,
            OLD.positive_support,
            OLD.negative_support,
            OLD.belief_confidence,
            OLD.preferred_proposition_id,
            OLD.evidence_count,
            OLD.independent_evidence_count,
            OLD.resolved_through_node_id,
            OLD.resolver_version
        ) IS NOT DISTINCT FROM ROW(
            NEW.stance,
            NEW.positive_support,
            NEW.negative_support,
            NEW.belief_confidence,
            NEW.preferred_proposition_id,
            NEW.evidence_count,
            NEW.independent_evidence_count,
            NEW.resolved_through_node_id,
            NEW.resolver_version
        ) THEN
            RETURN NEW;
        END IF;
        v_instance_id := NEW.instance_id;
        v_atom_id := NEW.atom_id;
    ELSIF TG_OP = 'INSERT' THEN
        v_instance_id := NEW.instance_id;
        v_atom_id := NEW.atom_id;
    ELSE
        v_instance_id := OLD.instance_id;
        v_atom_id := OLD.atom_id;
    END IF;

    INSERT INTO aios.rdf_character_belief_dirty (
        instance_id, atom_id, dirty_version, dirty_at
    )
    VALUES (v_instance_id, v_atom_id, 1, now())
    ON CONFLICT (instance_id, atom_id) DO UPDATE
    SET dirty_version=aios.rdf_character_belief_dirty.dirty_version + 1,
        dirty_at=now();

    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_character_belief_rdf_dirty
    ON aios.character_belief_state;
CREATE TRIGGER trg_character_belief_rdf_dirty
AFTER INSERT OR UPDATE OR DELETE ON aios.character_belief_state
FOR EACH ROW
EXECUTE FUNCTION aios.mark_character_belief_rdf_dirty();

CREATE OR REPLACE FUNCTION aios.mark_character_belief_rdf_dirty_for_atom()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(OLD.subject_norm, OLD.predicate_norm, OLD.object_norm)
       IS NOT DISTINCT FROM
       ROW(NEW.subject_norm, NEW.predicate_norm, NEW.object_norm) THEN
        RETURN NEW;
    END IF;

    INSERT INTO aios.rdf_character_belief_dirty (
        instance_id, atom_id, dirty_version, dirty_at
    )
    SELECT bs.instance_id, bs.atom_id, 1, now()
    FROM aios.character_belief_state bs
    WHERE bs.atom_id=NEW.atom_id
    ON CONFLICT (instance_id, atom_id) DO UPDATE
    SET dirty_version=aios.rdf_character_belief_dirty.dirty_version + 1,
        dirty_at=now();

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_atom_belief_rdf_dirty
    ON aios.semantic_atom;
CREATE TRIGGER trg_semantic_atom_belief_rdf_dirty
AFTER UPDATE OF subject_norm, predicate_norm, object_norm ON aios.semantic_atom
FOR EACH ROW
EXECUTE FUNCTION aios.mark_character_belief_rdf_dirty_for_atom();

-- Existing databases need one incremental pass. This does not force a full
-- graph rewrite: each current belief is simply placed in the same dirty set.
INSERT INTO aios.rdf_character_belief_dirty (
    instance_id, atom_id, dirty_version, dirty_at
)
SELECT instance_id, atom_id, 1, now()
FROM aios.character_belief_state
ON CONFLICT (instance_id, atom_id) DO NOTHING;

COMMIT;
