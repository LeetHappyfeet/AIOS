-- AIOS semantic validation decision graph
-- Derived semantic decisions are revisable. New evidence invalidates only the
-- decisions that declared a dependency on it; downstream decisions may depend
-- on earlier decisions through evidence_type='decision'.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_validation_decision (
    decision_type text NOT NULL,
    decision_key text NOT NULL,
    subject_type text NOT NULL,
    subject_key text NOT NULL,
    proposed_value text,
    selected_value text,
    status text NOT NULL CHECK (status IN (
        'verified','protected_explicit','fragile','challenged',
        'insufficient_evidence','single_plausible_owner','stale'
    )),
    resolver_version text NOT NULL,
    winner_score integer NOT NULL DEFAULT 0,
    runner_up_score integer NOT NULL DEFAULT 0,
    margin integer NOT NULL DEFAULT 0,
    stability double precision NOT NULL DEFAULT 0.0 CHECK (stability BETWEEN 0 AND 1),
    matrix jsonb NOT NULL DEFAULT '{}'::jsonb,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision integer NOT NULL DEFAULT 1,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    stale_at timestamptz,
    PRIMARY KEY (decision_type, decision_key)
);

CREATE INDEX IF NOT EXISTS idx_semantic_validation_subject
    ON aios.semantic_validation_decision (subject_type, subject_key);
CREATE INDEX IF NOT EXISTS idx_semantic_validation_stale
    ON aios.semantic_validation_decision (status, stale_at)
    WHERE status='stale';

CREATE TABLE IF NOT EXISTS aios.semantic_validation_dependency (
    decision_type text NOT NULL,
    decision_key text NOT NULL,
    evidence_type text NOT NULL,
    evidence_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (decision_type, decision_key, evidence_type, evidence_key),
    FOREIGN KEY (decision_type, decision_key)
        REFERENCES aios.semantic_validation_decision(decision_type, decision_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_semantic_validation_evidence
    ON aios.semantic_validation_dependency (evidence_type, evidence_key);

COMMENT ON TABLE aios.semantic_validation_decision IS
'Revisable adversarial-matrix decisions. These rows describe derived interpretation, not source truth.';
COMMENT ON TABLE aios.semantic_validation_dependency IS
'Explicit evidence dependency graph used for bounded recursive semantic invalidation when later evidence arrives.';

CREATE OR REPLACE FUNCTION aios.mark_semantic_validation_stale(
    p_evidence_type text,
    p_evidence_key text
) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type
          AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    )
    UPDATE aios.semantic_validation_decision v
    SET status='stale', stale_at=COALESCE(v.stale_at, now())
    FROM affected a
    WHERE v.decision_type=a.decision_type
      AND v.decision_key=a.decision_key
      AND v.status <> 'stale';

    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    ), claims AS (
        SELECT DISTINCT v.subject_key
        FROM affected a
        JOIN aios.semantic_validation_decision v
          ON v.decision_type=a.decision_type AND v.decision_key=a.decision_key
        WHERE v.decision_type IN ('semantic_owner','world_assignment','entity_referent')
          AND v.subject_type='claim'
    )
    UPDATE aios.semantic_topology_projection stp
    SET projected_at=NULL,
        updated_at=now(),
        meta=stp.meta || jsonb_build_object('reproject_reason','semantic_validation_stale')
    FROM claims c
    WHERE stp.claim_id::text=c.subject_key
      AND stp.projected_at IS NOT NULL;

    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    ), pairs AS (
        SELECT DISTINCT v.subject_key
        FROM affected a
        JOIN aios.semantic_validation_decision v
          ON v.decision_type=a.decision_type AND v.decision_key=a.decision_key
        WHERE v.decision_type IN ('proposition_relation','event_identity')
    )
    DELETE FROM aios.semantic_neighbor_relation r
    USING pairs p
    WHERE (r.proposition_id::text || ':' || r.neighbor_proposition_id::text)=p.subject_key
       OR (r.neighbor_proposition_id::text || ':' || r.proposition_id::text)=p.subject_key;

    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    ), assertions AS (
        SELECT DISTINCT v.subject_key
        FROM affected a
        JOIN aios.semantic_validation_decision v
          ON v.decision_type=a.decision_type AND v.decision_key=a.decision_key
        WHERE v.decision_type='epistemic_promotion'
    )
    UPDATE aios.world_proposition_assertion a
    SET last_checked_at=NULL,
        epistemic_status=CASE
            WHEN a.source_kind='generated_fill' AND a.epistemic_status='corroborated'
            THEN 'provisional'
            ELSE a.epistemic_status
        END,
        updated_at=now()
    FROM assertions s
    WHERE a.assertion_id::text=s.subject_key;
END;
$$;

CREATE OR REPLACE FUNCTION aios.semantic_validation_context_trigger()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM aios.mark_semantic_validation_stale('claim_context', NEW.claim_id::text);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_validation_claim_context ON aios.claim_context_resolution;
CREATE TRIGGER trg_semantic_validation_claim_context
AFTER INSERT OR UPDATE ON aios.claim_context_resolution
FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_context_trigger();

CREATE OR REPLACE FUNCTION aios.semantic_validation_neighbor_trigger()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    pair_forward text;
    pair_reverse text;
BEGIN
    pair_forward := NEW.proposition_id::text || ':' || NEW.neighbor_proposition_id::text;
    pair_reverse := NEW.neighbor_proposition_id::text || ':' || NEW.proposition_id::text;
    PERFORM aios.mark_semantic_validation_stale('semantic_neighbors', NEW.proposition_id::text);
    PERFORM aios.mark_semantic_validation_stale('semantic_neighbors', NEW.neighbor_proposition_id::text);
    PERFORM aios.mark_semantic_validation_stale('semantic_neighbor', pair_forward);
    PERFORM aios.mark_semantic_validation_stale('semantic_neighbor', pair_reverse);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_validation_neighbor ON aios.semantic_neighbor_candidate;
CREATE TRIGGER trg_semantic_validation_neighbor
AFTER INSERT OR UPDATE OF similarity, status ON aios.semantic_neighbor_candidate
FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_neighbor_trigger();

CREATE OR REPLACE FUNCTION aios.semantic_validation_conflict_trigger()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    pair_forward text;
    pair_reverse text;
BEGIN
    pair_forward := NEW.proposition_a_id::text || ':' || NEW.proposition_b_id::text;
    pair_reverse := NEW.proposition_b_id::text || ':' || NEW.proposition_a_id::text;
    PERFORM aios.mark_semantic_validation_stale('proposition_conflict', NEW.proposition_a_id::text);
    PERFORM aios.mark_semantic_validation_stale('proposition_conflict', NEW.proposition_b_id::text);
    PERFORM aios.mark_semantic_validation_stale('proposition_conflict', pair_forward);
    PERFORM aios.mark_semantic_validation_stale('proposition_conflict', pair_reverse);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_validation_conflict ON aios.proposition_conflict;
CREATE TRIGGER trg_semantic_validation_conflict
AFTER INSERT OR UPDATE OF conflict_type, strength ON aios.proposition_conflict
FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_conflict_trigger();

CREATE OR REPLACE FUNCTION aios.semantic_validation_world_assertion_trigger()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_kind='generated_fill'
       AND COALESCE(NEW.meta->>'resolution','') IN (
           'adversarial_corroboration','adversarial_supersession','remain_provisional'
       )
    THEN
        RETURN NEW;
    END IF;

    PERFORM aios.mark_semantic_validation_stale(
        'world_proposition', NEW.world_id::text || ':' || NEW.proposition_id::text
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_validation_world_assertion ON aios.world_proposition_assertion;
CREATE TRIGGER trg_semantic_validation_world_assertion
AFTER INSERT OR UPDATE OF epistemic_status, confidence, superseded_by_assertion_id
ON aios.world_proposition_assertion
FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_world_assertion_trigger();

CREATE OR REPLACE FUNCTION aios.semantic_validation_character_identity_trigger()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM aios.mark_semantic_validation_stale('character_identity', lower(trim(NEW.character_id)));
    IF NEW.display_name IS NOT NULL THEN
        PERFORM aios.mark_semantic_validation_stale('character_identity', lower(trim(NEW.display_name)));
    END IF;
    IF NEW.canonical_name IS NOT NULL THEN
        PERFORM aios.mark_semantic_validation_stale('character_identity', lower(trim(NEW.canonical_name)));
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_validation_character_identity ON aios.character_identity;
CREATE TRIGGER trg_semantic_validation_character_identity
AFTER INSERT OR UPDATE OF character_id, display_name, canonical_name ON aios.character_identity
FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_character_identity_trigger();

CREATE OR REPLACE FUNCTION aios.semantic_validation_character_alias_trigger()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM aios.mark_semantic_validation_stale('character_identity', lower(trim(NEW.alias)));
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_validation_character_alias ON aios.character_alias;
CREATE TRIGGER trg_semantic_validation_character_alias
AFTER INSERT OR UPDATE OF alias, character_id ON aios.character_alias
FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_character_alias_trigger();

CREATE OR REPLACE FUNCTION aios.semantic_validation_decision_trigger()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        PERFORM aios.mark_semantic_validation_stale(
            'decision', NEW.decision_type || ':' || NEW.decision_key
        );
    ELSIF OLD.selected_value IS DISTINCT FROM NEW.selected_value
       OR (OLD.status IS DISTINCT FROM NEW.status AND NEW.status <> 'stale')
    THEN
        PERFORM aios.mark_semantic_validation_stale(
            'decision', NEW.decision_type || ':' || NEW.decision_key
        );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_validation_decision ON aios.semantic_validation_decision;
CREATE TRIGGER trg_semantic_validation_decision
AFTER INSERT OR UPDATE OF selected_value, status ON aios.semantic_validation_decision
FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_decision_trigger();

COMMIT;
