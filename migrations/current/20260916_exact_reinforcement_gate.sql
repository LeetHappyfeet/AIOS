-- Exact semantic reinforcement gate
-- 2026-09-16
--
-- A normalized proposition is already content-addressed by proposition_hash.
-- Re-observing the same proposition in the same epistemic scope is valuable
-- evidence, but it does not deserve another observation-shaped RDF projection
-- or another claim-topology expansion.  Preserve the observation/provenance and
-- let the existing belief/world-memory aggregators consume it, while recording
-- durable completion receipts for the expensive projections we intentionally
-- suppress.
--
-- This gate is deliberately conservative: it only fires on exact proposition
-- identity after semantic normalization and only inside the same resolved scope.
-- Near matches, refinements, contradictions, temporal changes, and unresolved
-- claims continue through the normal pipeline.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_exact_admission (
    claim_id uuid PRIMARY KEY
        REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL
        REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    matched_claim_id uuid
        REFERENCES aios.claim_candidate(claim_id) ON DELETE SET NULL,
    scope_key text NOT NULL,
    decision text NOT NULL
        CHECK (decision IN ('novel_exact','reinforces_exact')),
    reason text NOT NULL,
    evidence_preserved boolean NOT NULL DEFAULT true,
    gate_version text NOT NULL DEFAULT 'exact-proposition-v1',
    decided_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_semantic_exact_admission_proposition_scope
    ON aios.semantic_exact_admission (proposition_id, scope_key, decision);

COMMENT ON TABLE aios.semantic_exact_admission IS
'Conservative post-normalization admission gate. Exact same-scope proposition repeats preserve evidence but may suppress redundant RDF observation and claim-topology expansion.';

CREATE OR REPLACE FUNCTION aios.exact_admission_scope_key(p_claim_id uuid)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN ccr.epistemic_scope='character'
             AND ccr.origin_character_id IS NOT NULL
             AND ccr.character_instance_id IS NOT NULL
            THEN 'char:' || ccr.origin_character_id
                 || ':instance:' || ccr.character_instance_id::text
        WHEN ccr.source_id IS NOT NULL
            THEN 'source:' || ccr.source_id
        WHEN ccr.world_id IS NOT NULL
            THEN 'world:' || ccr.world_id::text || ':observed'
        ELSE 'claim:' || ccr.claim_id::text
    END
    FROM aios.claim_context_resolution ccr
    WHERE ccr.claim_id=p_claim_id
    ORDER BY ccr.resolved_at DESC
    LIMIT 1
$$;

CREATE OR REPLACE FUNCTION aios.apply_exact_semantic_admission(p_claim_id uuid)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_proposition_id uuid;
    v_scope_key text;
    v_matched_claim_id uuid;
    v_decision text;
BEGIN
    SELECT o.proposition_id, aios.exact_admission_scope_key(o.claim_id)
    INTO v_proposition_id, v_scope_key
    FROM aios.observation o
    WHERE o.claim_id=p_claim_id;

    IF v_proposition_id IS NULL OR v_scope_key IS NULL THEN
        RETURN 'unresolved';
    END IF;

    -- Exact proposition identity is backed by proposition_hash uniqueness in
    -- aios.proposition.  Scope equality prevents a fact observed in one
    -- character/source/world from suppressing a distinct epistemic acquisition.
    SELECT o.claim_id
    INTO v_matched_claim_id
    FROM aios.observation o
    WHERE o.proposition_id=v_proposition_id
      AND o.claim_id<>p_claim_id
      AND aios.exact_admission_scope_key(o.claim_id)=v_scope_key
    ORDER BY o.observed_at, o.observation_id
    LIMIT 1;

    v_decision := CASE
        WHEN v_matched_claim_id IS NULL THEN 'novel_exact'
        ELSE 'reinforces_exact'
    END;

    INSERT INTO aios.semantic_exact_admission (
        claim_id, proposition_id, matched_claim_id, scope_key,
        decision, reason, evidence_preserved, gate_version, decided_at, meta
    )
    VALUES (
        p_claim_id,
        v_proposition_id,
        v_matched_claim_id,
        v_scope_key,
        v_decision,
        CASE
            WHEN v_matched_claim_id IS NULL THEN 'no_same_scope_exact_proposition'
            ELSE 'same_scope_exact_proposition_already_observed'
        END,
        true,
        'exact-proposition-v1',
        now(),
        jsonb_build_object(
            'matched_claim_id', v_matched_claim_id,
            'suppresses_rdf_observation', v_matched_claim_id IS NOT NULL,
            'suppresses_claim_topology', v_matched_claim_id IS NOT NULL
        )
    )
    ON CONFLICT (claim_id) DO UPDATE
    SET proposition_id=EXCLUDED.proposition_id,
        matched_claim_id=EXCLUDED.matched_claim_id,
        scope_key=EXCLUDED.scope_key,
        decision=EXCLUDED.decision,
        reason=EXCLUDED.reason,
        evidence_preserved=true,
        gate_version=EXCLUDED.gate_version,
        decided_at=now(),
        meta=EXCLUDED.meta;

    IF v_decision='reinforces_exact' THEN
        -- The supervisor treats this receipt as completion of the world
        -- epistemic observation projection.  No RDF triple is written: the
        -- observation remains authoritative SQL evidence and the existing
        -- consolidated memory state is allowed to absorb/reweight it.
        INSERT INTO aios.rdf_promotion_log (
            claim_id, rdf_dataset, rdf_graph, rdf_subject,
            rdf_predicate, rdf_object, promoted_by, promoted_at,
            promotion_meta
        )
        VALUES (
            p_claim_id,
            'world',
            'urn:aios:world:epistemic',
            'urn:aios:world:observation:' || p_claim_id::text,
            'world:observesProposition',
            'suppressed:exact-reinforcement',
            'exact_semantic_admission_gate',
            now(),
            jsonb_build_object(
                'suppressed', true,
                'reason', 'same_scope_exact_proposition_already_observed',
                'matched_claim_id', v_matched_claim_id,
                'evidence_preserved', true,
                'gate_version', 'exact-proposition-v1'
            )
        )
        ON CONFLICT (claim_id, rdf_dataset, rdf_graph, rdf_predicate)
        DO UPDATE SET
            rdf_object=EXCLUDED.rdf_object,
            promoted_by=EXCLUDED.promoted_by,
            promoted_at=EXCLUDED.promoted_at,
            promotion_meta=EXCLUDED.promotion_meta;

        -- Likewise mark claim topology complete without manufacturing duplicate
        -- topology nodes/edges.  Character acquisition topology is intentionally
        -- left alone in this first gate: it has distinct epistemic semantics and
        -- can be consolidated separately after measuring this change.
        INSERT INTO aios.semantic_topology_projection (
            projection_key, claim_id, scope_key, rdf_dataset, rdf_graph,
            resolver_version, projected_at, last_error, meta
        )
        VALUES (
            'claim:' || p_claim_id::text || ':exact-reinforcement',
            p_claim_id,
            v_scope_key,
            'none',
            'urn:aios:suppressed:exact-reinforcement',
            'semantic-topology-v1',
            now(),
            NULL,
            jsonb_build_object(
                'suppressed', true,
                'reason', 'same_scope_exact_proposition_already_observed',
                'matched_claim_id', v_matched_claim_id,
                'evidence_preserved', true,
                'gate_version', 'exact-proposition-v1'
            )
        )
        ON CONFLICT (projection_key) DO UPDATE
        SET projected_at=EXCLUDED.projected_at,
            last_error=NULL,
            meta=EXCLUDED.meta,
            updated_at=now();
    END IF;

    RETURN v_decision;
END;
$$;

CREATE OR REPLACE FUNCTION aios.trg_apply_exact_semantic_admission()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM aios.apply_exact_semantic_admission(NEW.claim_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_apply_exact_semantic_admission ON aios.observation;
CREATE TRIGGER trg_apply_exact_semantic_admission
AFTER INSERT OR UPDATE OF proposition_id ON aios.observation
FOR EACH ROW
EXECUTE FUNCTION aios.trg_apply_exact_semantic_admission();

-- Existing observations are intentionally not backfilled here.  This is an
-- admission gate for new work; a mass historical backfill could create a large
-- write burst and should only be done as an explicit maintenance operation.

COMMIT;
