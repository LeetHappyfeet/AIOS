-- AIOS semantic memory reconciliation stage.
BEGIN;

-- This is the boundary between normalized evidence and current semantic memory.
-- /char and /world are deliberately independent projections. Evidence remains
-- in the observation/proposition/acquisition substrate and is never collapsed
-- by deleting provenance.

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
'Convergent current /world semantic memory. It is populated only from world-scoped evidence; character belief evidence is excluded.';

-- ---------------------------------------------------------------------------
-- Reconciliation audit boundary.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aios.memory_reconciliation_receipt (
    claim_id uuid PRIMARY KEY
        REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
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
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_reconciliation_receipt_scope
    ON aios.memory_reconciliation_receipt (path, scope_key, updated_at DESC);

COMMENT ON TABLE aios.memory_reconciliation_receipt IS
'Audit boundary between normalized semantic evidence and durable memory promotion. Each claim is routed to /char, /world, or evidence-only without cross-projecting state.';

-- ---------------------------------------------------------------------------
-- /world materializer.
--
-- Notice the hard scope predicate below. This function never reads
-- knowledge_acquisition_event or character_proposition_knowledge. A character
-- believing P is therefore insufficient to make P objective world memory.
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
    v_resolved_through_node_id uuid;
BEGIN
    WITH evidence AS (
        SELECT
            p.proposition_id,
            p.polarity,
            o.observation_id,
            o.dag_node_id,
            LEAST(
                0.999999,
                GREATEST(
                    0.0,
                    COALESCE(pe.source_weight, 0.5)
                    * COALESCE(pe.confidence, o.extraction_confidence, 0.5)
                )
            ) AS weight,
            COALESCE(o.source_key, 'unknown') || ':' ||
                COALESCE(o.dag_node_id::text, o.observation_id::text) AS correlation_key,
            o.observed_at
        FROM aios.observation o
        JOIN aios.proposition p
          ON p.proposition_id=o.proposition_id
        JOIN aios.claim_context_resolution ccr
          ON ccr.claim_id=o.claim_id
        LEFT JOIN aios.proposition_evidence pe
          ON pe.proposition_id=p.proposition_id
         AND pe.observation_id=o.observation_id
         AND pe.evidence_role='support'
        LEFT JOIN aios.dag_node dn
          ON dn.node_id=o.dag_node_id
        LEFT JOIN aios.ingest_event ie
          ON ie.event_id=dn.event_id
        WHERE p.atom_id=p_atom_id
          AND ccr.world_id=p_world_id
          AND ccr.epistemic_scope='world'
          AND (ie.event_id IS NULL OR ie.superseded_at IS NULL)
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
        p.proposition_id,
        o.dag_node_id
    INTO
        v_preferred_proposition_id,
        v_resolved_through_node_id
    FROM aios.observation o
    JOIN aios.proposition p
      ON p.proposition_id=o.proposition_id
    JOIN aios.claim_context_resolution ccr
      ON ccr.claim_id=o.claim_id
    LEFT JOIN aios.proposition_evidence pe
      ON pe.proposition_id=p.proposition_id
     AND pe.observation_id=o.observation_id
     AND pe.evidence_role='support'
    LEFT JOIN aios.dag_node dn
      ON dn.node_id=o.dag_node_id
    LEFT JOIN aios.ingest_event ie
      ON ie.event_id=dn.event_id
    WHERE p.atom_id=p_atom_id
      AND ccr.world_id=p_world_id
      AND ccr.epistemic_scope='world'
      AND (ie.event_id IS NULL OR ie.superseded_at IS NULL)
    ORDER BY
        CASE
            WHEN v_stance='positive' AND p.polarity=1 THEN 0
            WHEN v_stance='negative' AND p.polarity=-1 THEN 0
            WHEN v_stance='unresolved' THEN 0
            ELSE 1
        END,
        (COALESCE(pe.source_weight, 0.5)
         * COALESCE(pe.confidence, o.extraction_confidence, 0.5)) DESC,
        o.observed_at DESC,
        p.proposition_id
    LIMIT 1;

    INSERT INTO aios.world_memory_state (
        world_id, atom_id, stance,
        positive_support, negative_support, state_confidence,
        preferred_proposition_id, evidence_count,
        independent_evidence_count, resolved_through_node_id,
        resolver_version, meta, resolved_at, updated_at
    )
    VALUES (
        p_world_id,
        p_atom_id,
        v_stance,
        v_positive_support,
        v_negative_support,
        v_state_confidence,
        v_preferred_proposition_id,
        v_evidence_count,
        v_independent_count,
        v_resolved_through_node_id,
        'memory-reconciliation-v1',
        jsonb_build_object(
            'path', 'world',
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
-- Route normalized claims at the memory boundary.
--
-- /char is intentionally not materialized here. Character normalization later
-- emits knowledge_acquisition_event, and the existing admission + belief
-- reconciliation triggers materialize character_belief_state after that
-- evidence has actually crossed the character acquisition boundary.
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

    ELSIF v_scope='world' AND v_world_id IS NOT NULL THEN
        v_path := 'world';
        v_scope_key := 'world:' || v_world_id::text;
        v_outcome := 'world_memory_reconciled';
        PERFORM aios.reconcile_world_memory_atom(v_world_id, v_atom_id);

    ELSE
        v_path := 'evidence';
        v_scope_key := CASE
            WHEN v_source_id IS NOT NULL THEN 'source:' || v_source_id
            WHEN v_world_id IS NOT NULL THEN 'world:' || v_world_id::text || ':evidence'
            ELSE 'claim:' || NEW.claim_id::text
        END;
        v_outcome := 'evidence_only';
    END IF;

    INSERT INTO aios.memory_reconciliation_receipt (
        claim_id, proposition_id, atom_id, path, scope_key,
        outcome, resolver_version, meta, reconciled_at, updated_at
    )
    VALUES (
        NEW.claim_id,
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

-- proposition_evidence is written just after observation during normalization.
-- Refresh /world once the final source/confidence weight is available.
CREATE OR REPLACE FUNCTION aios.refresh_world_memory_from_proposition_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_world_id uuid;
    v_atom_id uuid;
    v_scope text;
BEGIN
    SELECT ccr.world_id, ccr.epistemic_scope, p.atom_id
    INTO v_world_id, v_scope, v_atom_id
    FROM aios.observation o
    JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
    JOIN aios.proposition p ON p.proposition_id=NEW.proposition_id
    WHERE o.observation_id=NEW.observation_id;

    IF v_scope='world' AND v_world_id IS NOT NULL AND v_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_world_memory_atom(v_world_id, v_atom_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_world_memory_from_proposition_evidence
ON aios.proposition_evidence;
CREATE TRIGGER trg_refresh_world_memory_from_proposition_evidence
AFTER INSERT OR UPDATE OF source_weight, confidence, evidence_role
ON aios.proposition_evidence
FOR EACH ROW
WHEN (NEW.evidence_role='support')
EXECUTE FUNCTION aios.refresh_world_memory_from_proposition_evidence();

-- If source events are superseded later, rebuild only affected world atoms.
CREATE OR REPLACE FUNCTION aios.refresh_world_memory_from_ingest_supersession()
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
        SELECT DISTINCT ccr.world_id, p.atom_id
        FROM aios.dag_node dn
        JOIN aios.observation o ON o.dag_node_id=dn.node_id
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
        WHERE dn.event_id=NEW.event_id
          AND ccr.epistemic_scope='world'
          AND ccr.world_id IS NOT NULL
          AND p.atom_id IS NOT NULL
    LOOP
        PERFORM aios.reconcile_world_memory_atom(rec.world_id, rec.atom_id);
    END LOOP;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_world_memory_from_ingest_supersession
ON aios.ingest_event;
CREATE TRIGGER trg_refresh_world_memory_from_ingest_supersession
AFTER UPDATE OF superseded_at
ON aios.ingest_event
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_world_memory_from_ingest_supersession();

COMMIT;
