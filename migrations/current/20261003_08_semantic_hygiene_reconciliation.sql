-- Semantic Hygiene Reconciliation V1.
-- All proposals are inert. Applying requires both an explicit source-verified
-- adjudication and a transaction-local opt-in. Never delete source history.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_hygiene_adjudication (
    adjudication_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_id uuid NOT NULL REFERENCES aios.semantic_hygiene_shadow_audit(audit_id),
    claim_id uuid NOT NULL REFERENCES aios.claim_candidate(claim_id),
    frame_id uuid NOT NULL REFERENCES aios.claim_semantic_frame(frame_id),
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id),
    source_revision_key text NOT NULL,
    validator_version text NOT NULL,
    source_text text NOT NULL,
    action text NOT NULL CHECK (action IN ('suppress_independent','demote_to_context')),
    status text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed','applied','superseded')),
    actor text,
    proposed_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz,
    UNIQUE (claim_id,frame_id,proposition_id,source_revision_key,validator_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_hygiene_one_applied_occurrence
 ON aios.semantic_hygiene_adjudication(claim_id,frame_id,proposition_id)
 WHERE status='applied';
CREATE INDEX IF NOT EXISTS idx_semantic_hygiene_applied_proposition
 ON aios.semantic_hygiene_adjudication(proposition_id) WHERE status='applied';

CREATE TABLE IF NOT EXISTS aios.semantic_hygiene_adjudication_event (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    adjudication_id uuid NOT NULL REFERENCES aios.semantic_hygiene_adjudication(adjudication_id),
    action text NOT NULL,
    actor text NOT NULL,
    source_revision_key text NOT NULL,
    event_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE aios.semantic_hygiene_adjudication_event IS
 'Append-only operator adjudication history. No historical claim or evidence row is deleted.';

-- Propose only a claim/frame/proposition present in the immutable shadow audit.
-- This does not change effective eligibility or knowledge.
CREATE OR REPLACE FUNCTION aios.propose_semantic_hygiene_adjudication(
    p_audit uuid, p_claim uuid, p_frame uuid, p_proposition uuid, p_action text
) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE v_id uuid;
BEGIN
    IF p_action NOT IN ('suppress_independent','demote_to_context') THEN
        RAISE EXCEPTION 'Unsupported hygiene action: %',p_action;
    END IF;

    INSERT INTO aios.semantic_hygiene_adjudication
      (audit_id,claim_id,frame_id,proposition_id,source_revision_key,
       validator_version,source_text,action)
    SELECT h.audit_id,cc.claim_id,f.frame_id,p.proposition_id,
           si.revision_key,si.validator_version,cc.raw_text,p_action
    FROM aios.semantic_hygiene_shadow_audit h
    JOIN aios.proposition p ON p.atom_id=h.atom_id AND p.proposition_id=p_proposition
    JOIN aios.observation o ON o.claim_id=p_claim AND o.proposition_id=p.proposition_id
    JOIN aios.observation_proposition op
      ON op.observation_id=o.observation_id AND op.proposition_id=p.proposition_id
    JOIN aios.claim_semantic_frame f
      ON f.frame_id=op.frame_id AND f.frame_id=p_frame AND f.claim_id=p_claim
    JOIN aios.claim_candidate cc ON cc.claim_id=f.claim_id
    JOIN aios.claim_semantic_integrity si ON si.claim_id=cc.claim_id
    WHERE h.audit_id=p_audit
      AND h.policy_version='semantic-hygiene-shadow-v1'
      AND ((p_action='suppress_independent' AND h.disposition='repair_candidate')
        OR (p_action='demote_to_context' AND h.disposition='demote_candidate'))
      AND h.source_claim_ids ? p_claim::text
      AND h.source_revision_keys ? si.revision_key
      AND si.status='valid' AND si.source_text=cc.raw_text
      AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(
              COALESCE(h.evidence_snapshot->'sources','[]'::jsonb)
          ) AS source(value)
          WHERE source.value->>'claim_id'=cc.claim_id::text
            AND source.value->>'frame_id'=f.frame_id::text
            AND source.value->>'proposition_id'=p.proposition_id::text
            AND source.value->>'revision_key'=si.revision_key
            AND source.value->>'validator_version'=si.validator_version
            AND source.value->>'raw_text'=cc.raw_text
      )
      AND EXISTS (
          SELECT 1 FROM aios.proposition_evidence pe
          WHERE pe.observation_id=o.observation_id
            AND pe.proposition_id=p.proposition_id
      )
    ON CONFLICT (claim_id,frame_id,proposition_id,source_revision_key,validator_version)
      DO NOTHING
    RETURNING adjudication_id INTO v_id;

    IF v_id IS NULL THEN
        SELECT adjudication_id INTO v_id
        FROM aios.semantic_hygiene_adjudication
        WHERE claim_id=p_claim AND frame_id=p_frame AND proposition_id=p_proposition
          AND audit_id=p_audit AND action=p_action;
    END IF;
    IF v_id IS NULL THEN
        RAISE EXCEPTION 'Audit/source/frame/proposition revision mismatch; no adjudication created';
    END IF;
    RETURN v_id;
END $$;

-- Version-bounded: a new source interpretation requires new adjudication.
-- It cannot automatically inherit a rejection from an old frame revision.
CREATE OR REPLACE FUNCTION aios.semantic_hygiene_occurrence_suppressed(
    p_claim uuid, p_frame uuid, p_proposition uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM aios.semantic_hygiene_adjudication a
        JOIN aios.claim_semantic_integrity si ON si.claim_id=a.claim_id
        JOIN aios.claim_candidate cc ON cc.claim_id=a.claim_id
        WHERE a.claim_id=p_claim AND a.frame_id=p_frame
          AND a.proposition_id=p_proposition AND a.status='applied'
          AND a.source_revision_key=si.revision_key
          AND a.validator_version=si.validator_version
          AND a.source_text=cc.raw_text AND si.source_text=cc.raw_text
    )
$$;

CREATE OR REPLACE FUNCTION aios.semantic_hygiene_acquisition_suppressed(
    p_acquisition uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM aios.knowledge_acquisition_event kae
        JOIN aios.observation o
          ON o.claim_id=kae.claim_id AND o.proposition_id=kae.proposition_id
        JOIN aios.observation_proposition op
          ON op.observation_id=o.observation_id AND op.proposition_id=o.proposition_id
        WHERE kae.acquisition_id=p_acquisition
          AND aios.semantic_hygiene_occurrence_suppressed(
              kae.claim_id,op.frame_id,kae.proposition_id)
    )
$$;

-- Runs alphabetically after trg_enforce_frame_interpretation_admission.
-- Later calls to recompute_semantic_evidence_admission cannot resurrect an
-- unchanged, source-adjudicated acquisition.
CREATE OR REPLACE FUNCTION aios.enforce_semantic_hygiene_admission()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF aios.semantic_hygiene_acquisition_suppressed(NEW.acquisition_id) THEN
        NEW.meta := COALESCE(NEW.meta,'{}'::jsonb) || jsonb_build_object(
            'hygiene_policy_version','semantic-hygiene-reconciliation-v1',
            'hygiene_previous_status',NEW.status,
            'hygiene_previous_reason',NEW.reason
        );
        NEW.status := 'suppressed';
        NEW.reason := 'hygiene_source_adjudicated';
        NEW.confidence := 0;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_zz_semantic_hygiene_admission
  ON aios.semantic_evidence_admission;
CREATE TRIGGER trg_zz_semantic_hygiene_admission
BEFORE INSERT OR UPDATE ON aios.semantic_evidence_admission
FOR EACH ROW EXECUTE FUNCTION aios.enforce_semantic_hygiene_admission();

-- Both topology derivation AND Qdrant eligibility must honor exact source
-- occurrences, not the existence of some other healthy frame under the claim.
CREATE OR REPLACE FUNCTION aios.semantic_occurrence_topology_eligible(
    requested_claim uuid, requested_proposition uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM aios.observation o
        JOIN aios.observation_proposition op ON op.observation_id=o.observation_id
        JOIN aios.claim_semantic_frame f ON f.frame_id=op.frame_id
        JOIN aios.semantic_interpretation si ON si.frame_id=f.frame_id
        WHERE o.claim_id=requested_claim AND op.proposition_id=requested_proposition
          AND f.claim_id=o.claim_id AND si.claim_id=o.claim_id
          AND f.resolution_status='resolved' AND si.standalone_semantic
          AND NOT aios.semantic_hygiene_occurrence_suppressed(
              o.claim_id,f.frame_id,op.proposition_id)
    )
$$;
CREATE OR REPLACE FUNCTION aios.semantic_proposition_topology_eligible(
    requested_proposition uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM aios.observation o
        JOIN aios.observation_proposition op ON op.observation_id=o.observation_id
        WHERE op.proposition_id=requested_proposition
          AND aios.semantic_occurrence_topology_eligible(o.claim_id,op.proposition_id)
    )
$$;

-- Incremental RDF projector is driven by dirty scope versions plus the
-- topology mutation outbox. Always register contraction even when no new node
-- will be upserted.
CREATE OR REPLACE FUNCTION aios.mark_semantic_hygiene_scope_dirty(p_scope text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE aios.semantic_scope_projection_state
    SET dirty_version=dirty_version+1,
        status='dirty',dirty_at=now(),
        first_dirty_at=CASE WHEN dirty_version>projected_version
            THEN COALESCE(first_dirty_at,now()) ELSE now() END,
        last_error=NULL,updated_at=now()
    WHERE scope_key=p_scope;
END $$;

-- In case a projection races the initial adjudication and finishes before the
-- deferred belief worker, every subsequent affected node deletion/update
-- increments the dirty version again.
CREATE OR REPLACE FUNCTION aios.dirty_adjudicated_topology_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.proposition_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM aios.semantic_hygiene_adjudication a
        WHERE a.proposition_id=OLD.proposition_id AND a.status='applied'
    ) THEN
        PERFORM aios.mark_semantic_hygiene_scope_dirty(OLD.scope_key);
    END IF;
    RETURN COALESCE(NEW,OLD);
END $$;
DROP TRIGGER IF EXISTS trg_hygiene_topology_mutation_dirty
  ON aios.semantic_topology_node;
CREATE TRIGGER trg_hygiene_topology_mutation_dirty
AFTER UPDATE OR DELETE ON aios.semantic_topology_node
FOR EACH ROW WHEN (OLD.proposition_id IS NOT NULL)
EXECUTE FUNCTION aios.dirty_adjudicated_topology_mutation();

-- The only mutation entry point. Requires explicit operator identity and
-- SET LOCAL aios.semantic_hygiene_apply_enabled='on' in the SAME transaction.
-- Does NOT delete any raw claim, observation, evidence, acquisition or atom.
CREATE OR REPLACE FUNCTION aios.apply_semantic_hygiene_adjudication(
    p_adjudication uuid, p_actor text
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    a aios.semantic_hygiene_adjudication%ROWTYPE;
    v_atom uuid;
    v_admissions integer := 0;
    v_affected integer := 0;
    r record;
BEGIN
    IF current_setting('aios.semantic_hygiene_apply_enabled',true)
       IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'Semantic hygiene retraction is disabled; explicit transaction-local opt-in required';
    END IF;
    IF NULLIF(btrim(COALESCE(p_actor,'')),'') IS NULL THEN
        RAISE EXCEPTION 'A named adjudicating operator is required';
    END IF;
    SELECT * INTO a FROM aios.semantic_hygiene_adjudication
    WHERE adjudication_id=p_adjudication FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Unknown hygiene adjudication'; END IF;
    IF a.status='applied' THEN
        RETURN jsonb_build_object('adjudication_id',a.adjudication_id,
                                  'already_applied',true);
    END IF;
    IF a.status<>'proposed' THEN
        RAISE EXCEPTION 'Only proposed, currently source-verified adjudications may be applied';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'hygiene:'||a.claim_id::text||':'||a.proposition_id::text,0));

    -- Recheck live source integrity/frame binding at the moment of application.
    PERFORM 1 FROM aios.claim_semantic_integrity si
    JOIN aios.claim_candidate cc ON cc.claim_id=si.claim_id
    JOIN aios.claim_semantic_frame f
      ON f.claim_id=cc.claim_id AND f.frame_id=a.frame_id
    JOIN aios.observation o
      ON o.claim_id=cc.claim_id AND o.proposition_id=a.proposition_id
    JOIN aios.observation_proposition op
      ON op.observation_id=o.observation_id
     AND op.proposition_id=a.proposition_id AND op.frame_id=f.frame_id
    JOIN aios.proposition_evidence pe
      ON pe.observation_id=o.observation_id AND pe.proposition_id=o.proposition_id
    WHERE si.claim_id=a.claim_id AND si.revision_key=a.source_revision_key
      AND si.validator_version=a.validator_version
      AND si.source_text=a.source_text AND cc.raw_text=a.source_text
      AND si.status='valid';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Source/frame/evidence revision changed; re-audit instead of retracting';
    END IF;

    SELECT atom_id INTO STRICT v_atom FROM aios.proposition
    WHERE proposition_id=a.proposition_id;

    UPDATE aios.semantic_hygiene_adjudication
    SET status='applied',actor=btrim(p_actor),applied_at=now()
    WHERE adjudication_id=a.adjudication_id;

    -- Before admission trigger also enforces suppression on future recomputes.
    UPDATE aios.semantic_evidence_admission sea
    SET status='suppressed',reason='hygiene_source_adjudicated',
        confidence=0,updated_at=now()
    FROM aios.knowledge_acquisition_event kae
    WHERE kae.acquisition_id=sea.acquisition_id
      AND kae.claim_id=a.claim_id AND kae.proposition_id=a.proposition_id
      AND (sea.status,sea.reason,sea.confidence)
          IS DISTINCT FROM ('suppressed','hygiene_source_adjudicated',0::double precision);
    GET DIAGNOSTICS v_admissions=ROW_COUNT;

    -- The existing trigger handles acquisitions with admission rows. This
    -- explicit descendant pass covers missing/deferred admissions as well.
    FOR r IN SELECT DISTINCT instance_id
             FROM aios.knowledge_acquisition_event
             WHERE claim_id=a.claim_id AND proposition_id=a.proposition_id
    LOOP
        PERFORM aios.mark_character_belief_dirty_descendants(r.instance_id,v_atom);
    END LOOP;

    -- Include legacy materialized beliefs that may not be represented by a
    -- complete ancestor-acquisition topology, without inventing false evidence.
    INSERT INTO aios.character_belief_reconciliation_dirty
      (instance_id,atom_id,dirty_version,dirty_at)
    SELECT instance_id,v_atom,1,now()
    FROM aios.character_belief_state WHERE atom_id=v_atom
    ON CONFLICT (instance_id,atom_id) DO UPDATE
    SET dirty_version=aios.character_belief_reconciliation_dirty.dirty_version+1,
        dirty_at=now();
    GET DIAGNOSTICS v_affected=ROW_COUNT;

    FOR r IN SELECT DISTINCT scope_key FROM aios.semantic_topology_node
             WHERE proposition_id=a.proposition_id
    LOOP
        PERFORM aios.mark_semantic_hygiene_scope_dirty(r.scope_key);
    END LOOP;

    INSERT INTO aios.semantic_hygiene_adjudication_event
      (adjudication_id,action,actor,source_revision_key)
    VALUES (a.adjudication_id,a.action,btrim(p_actor),a.source_revision_key);

    RETURN jsonb_build_object(
        'adjudication_id',a.adjudication_id,'source_claim_id',a.claim_id,
        'proposition_id',a.proposition_id,'atom_id',v_atom,
        'admissions_updated',v_admissions,
        'belief_coordinates_dirtied',v_affected,
        'raw_evidence_preserved',true,
        'next','reconcile dirty beliefs; run topology-quarantine; verify RDF parity'
    );
END $$;
COMMIT;
