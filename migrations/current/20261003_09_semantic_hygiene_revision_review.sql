-- Semantic Hygiene Reconciliation V1: revised-source review and supervised release.
-- A changed revision is not silently admitted merely because the old exact
-- suppression fingerprint no longer matches. It remains unresolved pending
-- source re-adjudication until an operator explicitly supersedes the decision.
BEGIN;

CREATE OR REPLACE FUNCTION aios.semantic_hygiene_occurrence_pending_review(
    p_claim uuid,p_frame uuid,p_proposition uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM aios.semantic_hygiene_adjudication a
        WHERE a.claim_id=p_claim AND a.frame_id=p_frame
          AND a.proposition_id=p_proposition AND a.status='applied'
          AND NOT aios.semantic_hygiene_occurrence_suppressed(
              p_claim,p_frame,p_proposition)
    )
$$;

CREATE OR REPLACE FUNCTION aios.semantic_hygiene_acquisition_pending_review(
    p_acquisition uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM aios.knowledge_acquisition_event kae
        JOIN aios.observation o
          ON o.claim_id=kae.claim_id AND o.proposition_id=kae.proposition_id
        JOIN aios.observation_proposition op
          ON op.observation_id=o.observation_id AND op.proposition_id=o.proposition_id
        WHERE kae.acquisition_id=p_acquisition
          AND aios.semantic_hygiene_occurrence_pending_review(
              kae.claim_id,op.frame_id,kae.proposition_id)
    )
$$;

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
    ELSIF aios.semantic_hygiene_acquisition_pending_review(NEW.acquisition_id) THEN
        NEW.meta := COALESCE(NEW.meta,'{}'::jsonb) || jsonb_build_object(
            'hygiene_policy_version','semantic-hygiene-reconciliation-v1',
            'hygiene_previous_status',NEW.status,
            'hygiene_previous_reason',NEW.reason
        );
        NEW.status := 'unresolved';
        NEW.reason := 'hygiene_revision_requires_review';
        NEW.confidence := 0;
    END IF;
    RETURN NEW;
END $$;

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
          AND NOT aios.semantic_hygiene_occurrence_pending_review(
              o.claim_id,f.frame_id,op.proposition_id)
    )
$$;

-- Explicit operator release after actual source/frame revalidation. No direct
-- changes to original evidence. Restores the ordinary admission resolver,
-- which may itself choose active/unresolved/suppressed for the revised source.
CREATE OR REPLACE FUNCTION aios.supersede_semantic_hygiene_adjudication(
    p_adjudication uuid,p_actor text
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    a aios.semantic_hygiene_adjudication%ROWTYPE;
    v_atom uuid;
    r record;
BEGIN
    IF current_setting('aios.semantic_hygiene_apply_enabled',true)
        IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'Semantic hygiene release is disabled; explicit transaction-local opt-in required';
    END IF;
    IF NULLIF(btrim(COALESCE(p_actor,'')),'') IS NULL THEN
        RAISE EXCEPTION 'Named adjudicating operator is required';
    END IF;
    SELECT * INTO a FROM aios.semantic_hygiene_adjudication
    WHERE adjudication_id=p_adjudication FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Unknown hygiene adjudication'; END IF;
    IF a.status='superseded' THEN
        RETURN jsonb_build_object('adjudication_id',a.adjudication_id,
                                  'already_superseded',true);
    END IF;
    IF a.status<>'applied' THEN
        RAISE EXCEPTION 'Only applied adjudications may be superseded';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'hygiene:'||a.claim_id::text||':'||a.proposition_id::text,0));

    PERFORM 1 FROM aios.claim_semantic_integrity si
    JOIN aios.claim_candidate cc ON cc.claim_id=si.claim_id
    WHERE si.claim_id=a.claim_id AND si.source_text=cc.raw_text
      AND si.status='valid'
      AND (si.revision_key,si.validator_version,cc.raw_text)
        IS DISTINCT FROM
          (a.source_revision_key,a.validator_version,a.source_text);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Current valid integrity revision has not changed; cannot release unchanged rejected source';
    END IF;

    SELECT atom_id INTO STRICT v_atom FROM aios.proposition
    WHERE proposition_id=a.proposition_id;

    UPDATE aios.semantic_hygiene_adjudication
    SET status='superseded'
    WHERE adjudication_id=a.adjudication_id;

    INSERT INTO aios.semantic_hygiene_adjudication_event
      (adjudication_id,action,actor,source_revision_key)
    VALUES (a.adjudication_id,'superseded',btrim(p_actor),a.source_revision_key);

    -- Recompute every directly acquired instance and descendants using the
    -- current resolved source, not the old rejected receipt.
    FOR r IN SELECT acquisition_id,instance_id
             FROM aios.knowledge_acquisition_event
             WHERE claim_id=a.claim_id AND proposition_id=a.proposition_id
    LOOP
        PERFORM aios.recompute_semantic_evidence_admission(r.acquisition_id);
        PERFORM aios.mark_character_belief_dirty_descendants(r.instance_id,v_atom);
    END LOOP;
    INSERT INTO aios.character_belief_reconciliation_dirty
       (instance_id,atom_id,dirty_version,dirty_at)
    SELECT instance_id,v_atom,1,now()
    FROM aios.character_belief_state WHERE atom_id=v_atom
    ON CONFLICT (instance_id,atom_id) DO UPDATE
    SET dirty_version=aios.character_belief_reconciliation_dirty.dirty_version+1,
        dirty_at=now();
    FOR r IN SELECT DISTINCT scope_key FROM aios.semantic_topology_node
             WHERE proposition_id=a.proposition_id
    LOOP
        PERFORM aios.mark_semantic_hygiene_scope_dirty(r.scope_key);
    END LOOP;

    RETURN jsonb_build_object('adjudication_id',a.adjudication_id,
        'status','superseded','new_source_review_required',true,
        'raw_evidence_preserved',true);
END $$;
COMMIT;
