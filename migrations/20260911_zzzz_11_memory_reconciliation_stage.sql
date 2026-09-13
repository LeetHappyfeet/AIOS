-- AIOS semantic memory reconciliation stage.
BEGIN;

-- This is the boundary between normalized evidence and current semantic memory.
-- /char and /world are deliberately independent projections:
--
--   /char  <- knowledge acquisition -> evidence admission -> belief state
--   /world <- explicit world_proposition_assertion -> world memory state
--
-- Raw observations, propositions, acquisitions, and assertions remain intact as
-- provenance. Reconciliation collapses current meaning; it does not erase history.

-- ---------------------------------------------------------------------------
-- Current /world memory.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aios.world_memory_state (
    world_id uuid NOT NULL
        REFERENCES aios.world(world_id) ON DELETE CASCADE,
    atom_id uuid NOT NULL
        REFERENCES aios.semantic_atom(atom_id) ON DELETE CASCADE,
    stance text NOT NULL CHECK (stance IN ('positive','negative','unresolved')),
    positive_support double precision NOT NULL DEFAULT 0.0
        CHECK (positive_support BETWEEN 0 AND 1),
    negative_support double precision NOT NULL DEFAULT 0.0
        CHECK (negative_support BETWEEN 0 AND 1),
    state_confidence double precision NOT NULL DEFAULT 0.0
        CHECK (state_confidence BETWEEN 0 AND 1),
    preferred_proposition_id uuid
        REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    preferred_assertion_id uuid
        REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE SET NULL,
    evidence_count integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    independent_evidence_count integer NOT NULL DEFAULT 0
        CHECK (independent_evidence_count >= 0),
    resolved_through_node_id uuid
        REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    resolver_version text NOT NULL DEFAULT 'memory-reconciliation-v1',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, atom_id)
);

CREATE INDEX IF NOT EXISTS idx_world_memory_state_world
    ON aios.world_memory_state (world_id, stance, state_confidence DESC);

COMMENT ON TABLE aios.world_memory_state IS
'Convergent current /world semantic memory. Only explicit world_proposition_assertion rows participate; /char evidence cannot promote itself into /world.';

-- ---------------------------------------------------------------------------
-- Reconciliation audit boundary.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aios.memory_reconciliation_receipt (
    receipt_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    claim_id uuid
        REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    assertion_id uuid
        REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL
        REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    atom_id uuid NOT NULL
        REFERENCES aios.semantic_atom(atom_id) ON DELETE CASCADE,
    path text NOT NULL CHECK (path IN ('char','world','evidence')),
    scope_key text NOT NULL,
    outcome text NOT NULL,
    resolver_version text NOT NULL DEFAULT 'memory-reconciliation-v1',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    reconciled_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(claim_id, assertion_id) = 1),
    UNIQUE (claim_id),
    UNIQUE (assertion_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_reconciliation_receipt_scope
    ON aios.memory_reconciliation_receipt (path, scope_key, updated_at DESC);

COMMENT ON TABLE aios.memory_reconciliation_receipt IS
'Audit boundary for current-memory routing. Normalized claims route to /char or evidence-only; explicit world assertions route independently to /world.';

-- ---------------------------------------------------------------------------
-- /world materializer.
--
-- world_proposition_assertion is already AIOS's explicit objective-state gate.
-- The materializer therefore consumes that table rather than treating raw
-- narrative/source observations as world truth. Character acquisitions are not
-- consulted here under any circumstances.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION aios.reconcile_world_memory_atom(
    p_world_id uuid,
    p_atom_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_positive_support double precision := 0.0;
    v_negative_support double precision := 0.0;
    v_evidence_count integer := 0;
    v_independent_count integer := 0;
    v_stance text;
    v_state_confidence double precision := 0.0;
    v_preferred_proposition_id uuid;
    v_preferred_assertion_id uuid;
    v_resolved_through_node_id uuid;
BEGIN
    WITH evidence AS (
        SELECT
            a.assertion_id,
            a.proposition_id,
            p.polarity,
            LEAST(0.999999, GREATEST(0.0, COALESCE(a.confidence, 0.0))) AS weight,
            COALESCE(a.source_kind, 'unknown') || ':' || a.assertion_id::text
                AS correlation_key,
            a.generated_at_node_id,
            a.updated_at
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p
          ON p.proposition_id=a.proposition_id
        WHERE a.world_id=p_world_id
          AND p.atom_id=p_atom_id
          AND a.epistemic_status NOT IN ('rejected','superseded')
    ),
    correlated AS (
        SELECT polarity, correlation_key, MAX(weight) AS weight
        FROM evidence
        GROUP BY polarity, correlation_key
    )
    SELECT
        COALESCE(
            1.0 - exp(SUM(ln(1.0 - weight)) FILTER (WHERE polarity=1)),
            0.0
        ),
        COALESCE(
            1.0 - exp(SUM(ln(1.0 - weight)) FILTER (WHERE polarity=-1)),
            0.0
        ),
        (SELECT COUNT(*) FROM evidence),
        COUNT(*)
    INTO
        v_positive_support,
        v_negative_support,
        v_evidence_count,
        v_independent_count
    FROM correlated;

    IF v_evidence_count = 0 THEN
        DELETE FROM aios.world_memory_state
        WHERE world_id=p_world_id AND atom_id=p_atom_id;
        RETURN;
    END IF;

    IF v_positive_support >= 0.60
       AND (v_positive_support - v_negative_support) >= 0.15 THEN
        v_stance := 'positive';
    ELSIF v_negative_support >= 0.60
       AND (v_negative_support - v_positive_support) >= 0.15 THEN
        v_stance := 'negative';
    ELSE
        v_stance := 'unresolved';
    END IF;

    v_state_confidence := LEAST(
        1.0,
        GREATEST(0.0, abs(v_positive_support - v_negative_support))
    );

    SELECT
        a.proposition_id,
        a.assertion_id,
        a.generated_at_node_id
    INTO
        v_preferred_proposition_id,
        v_preferred_assertion_id,
        v_resolved_through_node_id
    FROM aios.world_proposition_assertion a
    JOIN aios.proposition p
      ON p.proposition_id=a.proposition_id
    WHERE a.world_id=p_world_id
      AND p.atom_id=p_atom_id
      AND a.epistemic_status NOT IN ('rejected','superseded')
    ORDER BY
        CASE
            WHEN v_stance='positive' AND p.polarity=1 THEN 0
            WHEN v_stance='negative' AND p.polarity=-1 THEN 0
            WHEN v_stance='unresolved' THEN 0
            ELSE 1
        END,
        a.confidence DESC,
        a.updated_at DESC,
        a.assertion_id
    LIMIT 1;

    INSERT INTO aios.world_memory_state (
        world_id, atom_id, stance,
        positive_support, negative_support, state_confidence,
        preferred_proposition_id, preferred_assertion_id,
        evidence_count, independent_evidence_count,
        resolved_through_node_id, resolver_version, meta,
        resolved_at, updated_at
    )
    VALUES (
        p_world_id,
        p_atom_id,
        v_stance,
        v_positive_support,
        v_negative_support,
        v_state_confidence,
        v_preferred_proposition_id,
        v_preferred_assertion_id,
        v_evidence_count,
        v_independent_count,
        v_resolved_through_node_id,
        'memory-reconciliation-v1',
        jsonb_build_object(
            'path', 'world',
            'authority_boundary', 'world_proposition_assertion',
            'evidence_topology_preserved', true,
            'accept_support', 0.60,
            'decision_margin', 0.15
        ),
        now(),
        now()
    )
    ON CONFLICT (world_id, atom_id) DO UPDATE
    SET stance=EXCLUDED.stance,
        positive_support=EXCLUDED.positive_support,
        negative_support=EXCLUDED.negative_support,
        state_confidence=EXCLUDED.state_confidence,
        preferred_proposition_id=EXCLUDED.preferred_proposition_id,
        preferred_assertion_id=EXCLUDED.preferred_assertion_id,
        evidence_count=EXCLUDED.evidence_count,
        independent_evidence_count=EXCLUDED.independent_evidence_count,
        resolved_through_node_id=EXCLUDED.resolved_through_node_id,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,
        resolved_at=now(),
        updated_at=now();
END;
$$;

-- ---------------------------------------------------------------------------
-- Normalized claim routing.
--
-- A normalized claim is NOT world truth. Character-scoped claims are routed to
-- the existing acquisition/admission/belief path. Everything else stays in the
-- evidence substrate unless a separate process explicitly creates a world
-- proposition assertion.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION aios.route_normalized_memory_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_atom_id uuid;
    v_scope text;
    v_world_id uuid;
    v_character_id text;
    v_instance_id uuid;
    v_source_id text;
    v_path text;
    v_scope_key text;
    v_outcome text;
BEGIN
    SELECT p.atom_id
    INTO v_atom_id
    FROM aios.proposition p
    WHERE p.proposition_id=NEW.proposition_id;

    SELECT
        ccr.epistemic_scope,
        ccr.world_id,
        ccr.origin_character_id,
        ccr.character_instance_id,
        ccr.source_id
    INTO
        v_scope,
        v_world_id,
        v_character_id,
        v_instance_id,
        v_source_id
    FROM aios.claim_context_resolution ccr
    WHERE ccr.claim_id=NEW.claim_id;

    IF v_atom_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF v_scope='character' AND v_instance_id IS NOT NULL THEN
        v_path := 'char';
        v_scope_key := 'char:' || COALESCE(v_character_id, v_instance_id::text);
        v_outcome := 'character_routed_to_acquisition_boundary';
    ELSE
        v_path := 'evidence';
        v_scope_key := CASE
            WHEN v_source_id IS NOT NULL THEN 'source:' || v_source_id
            WHEN v_scope='narrative' AND v_world_id IS NOT NULL
                THEN 'world:' || v_world_id::text || ':narrative-evidence'
            WHEN v_world_id IS NOT NULL
                THEN 'world:' || v_world_id::text || ':evidence'
            ELSE 'claim:' || NEW.claim_id::text
        END;
        v_outcome := 'evidence_only';
    END IF;

    INSERT INTO aios.memory_reconciliation_receipt (
        claim_id, assertion_id, proposition_id, atom_id, path, scope_key,
        outcome, resolver_version, meta, reconciled_at, updated_at
    )
    VALUES (
        NEW.claim_id,
        NULL,
        NEW.proposition_id,
        v_atom_id,
        v_path,
        v_scope_key,
        v_outcome,
        'memory-reconciliation-v1',
        jsonb_build_object(
            'epistemic_scope', v_scope,
            'world_id', v_world_id,
            'character_id', v_character_id,
            'character_instance_id', v_instance_id,
            'world_promotion', false,
            'evidence_topology_preserved', true
        ),
        now(),
        now()
    )
    ON CONFLICT (claim_id) DO UPDATE
    SET proposition_id=EXCLUDED.proposition_id,
        atom_id=EXCLUDED.atom_id,
        path=EXCLUDED.path,
        scope_key=EXCLUDED.scope_key,
        outcome=EXCLUDED.outcome,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,
        reconciled_at=now(),
        updated_at=now();

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_route_normalized_memory_evidence
ON aios.observation;
CREATE TRIGGER trg_route_normalized_memory_evidence
AFTER INSERT OR UPDATE OF proposition_id
ON aios.observation
FOR EACH ROW
EXECUTE FUNCTION aios.route_normalized_memory_evidence();

-- ---------------------------------------------------------------------------
-- Explicit /world assertion routing.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION aios.refresh_world_memory_from_assertion()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_old_atom_id uuid;
    v_new_atom_id uuid;
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN
        SELECT atom_id INTO v_old_atom_id
        FROM aios.proposition
        WHERE proposition_id=OLD.proposition_id;
    END IF;

    IF TG_OP IN ('INSERT','UPDATE') THEN
        SELECT atom_id INTO v_new_atom_id
        FROM aios.proposition
        WHERE proposition_id=NEW.proposition_id;
    END IF;

    -- An assertion can theoretically move between worlds/atoms during repair.
    -- Reconcile the old coordinate first so no stale current state is stranded.
    IF TG_OP='DELETE' THEN
        IF v_old_atom_id IS NOT NULL THEN
            PERFORM aios.reconcile_world_memory_atom(OLD.world_id, v_old_atom_id);
        END IF;
        DELETE FROM aios.memory_reconciliation_receipt
        WHERE assertion_id=OLD.assertion_id;
        RETURN OLD;
    END IF;

    IF TG_OP='UPDATE'
       AND v_old_atom_id IS NOT NULL
       AND (OLD.world_id IS DISTINCT FROM NEW.world_id
            OR v_old_atom_id IS DISTINCT FROM v_new_atom_id) THEN
        PERFORM aios.reconcile_world_memory_atom(OLD.world_id, v_old_atom_id);
    END IF;

    IF v_new_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_world_memory_atom(NEW.world_id, v_new_atom_id);

        INSERT INTO aios.memory_reconciliation_receipt (
            claim_id, assertion_id, proposition_id, atom_id, path, scope_key,
            outcome, resolver_version, meta, reconciled_at, updated_at
        )
        VALUES (
            NULL,
            NEW.assertion_id,
            NEW.proposition_id,
            v_new_atom_id,
            'world',
            'world:' || NEW.world_id::text,
            CASE
                WHEN NEW.epistemic_status IN ('rejected','superseded')
                    THEN 'world_assertion_excluded'
                ELSE 'world_memory_reconciled'
            END,
            'memory-reconciliation-v1',
            jsonb_build_object(
                'authority_boundary', 'world_proposition_assertion',
                'epistemic_status', NEW.epistemic_status,
                'source_kind', NEW.source_kind,
                'confidence', NEW.confidence,
                'evidence_topology_preserved', true
            ),
            now(),
            now()
        )
        ON CONFLICT (assertion_id) DO UPDATE
        SET proposition_id=EXCLUDED.proposition_id,
            atom_id=EXCLUDED.atom_id,
            path=EXCLUDED.path,
            scope_key=EXCLUDED.scope_key,
            outcome=EXCLUDED.outcome,
            resolver_version=EXCLUDED.resolver_version,
            meta=EXCLUDED.meta,
            reconciled_at=now(),
            updated_at=now();
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_world_memory_from_assertion
ON aios.world_proposition_assertion;
CREATE TRIGGER trg_refresh_world_memory_from_assertion
AFTER INSERT OR UPDATE OR DELETE
ON aios.world_proposition_assertion
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_world_memory_from_assertion();

COMMIT;
