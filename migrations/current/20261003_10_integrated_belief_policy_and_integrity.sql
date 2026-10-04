-- Post-baseline integration repair: live policy, source eligibility and runtime contract.
-- Old source/knowledge receipts stay immutable. Applying this migration DOES
-- NOT bulk promote, retract, or reconcile existing belief rows. Backfill is
-- an explicit bounded operation after the active experiment finishes.
BEGIN;

-- The late authority-lineage override still expects this legacy default.
-- Seed it from the already installed UNKNOWN family policy, not a second
-- independent interpretation of the thresholds.
INSERT INTO aios.belief_reconciliation_policy
  (policy_key,accept_support,decision_margin,resolver_version)
SELECT 'default',accept_support,decision_margin,'character-belief-v4-authority-family'
FROM aios.reconciliation_family_policy WHERE predicate_family='UNKNOWN'
ON CONFLICT(policy_key) DO NOTHING;

CREATE OR REPLACE FUNCTION aios.assert_belief_policy_configuration()
RETURNS void LANGUAGE plpgsql STABLE AS $$
DECLARE v_missing text[];
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM aios.belief_reconciliation_policy
      WHERE policy_key='default'
        AND accept_support IS NOT NULL AND decision_margin IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'AIOS missing belief_reconciliation_policy.default; migration/reference data incomplete';
  END IF;
  SELECT array_agg(f.predicate_family ORDER BY f.predicate_family) INTO v_missing
  FROM (VALUES ('UNKNOWN'),('IDENTITY'),('SOCIAL'),('MEMBERSHIP'),
      ('POSSESSION'),('EPISTEMIC'),('MEMORY'),('CAUSAL'),
      ('COMMUNICATION'),('ACTION'),('TEMPORAL'),('DESCRIPTIVE'),
      ('EMOTIONAL'),('GOAL'),('SPATIAL'),('RULE')) AS f(predicate_family)
  LEFT JOIN aios.reconciliation_family_policy configured
    ON configured.predicate_family=f.predicate_family
  WHERE configured.predicate_family IS NULL;
  IF v_missing IS NOT NULL THEN
    RAISE EXCEPTION 'AIOS missing reconciliation_family_policy rows: %',v_missing;
  END IF;
END $$;
SELECT aios.assert_belief_policy_configuration();

-- A current source receipt must still describe the current claim, containing
-- source paragraph and each V2 frame. Historical V3 receipts with no saved
-- source_section_digest remain legacy-eligible only if all the other identity
-- and frame checks pass. Fresh V4 receipts always record the digest.
ALTER TABLE aios.claim_semantic_integrity
  ADD COLUMN IF NOT EXISTS source_section_digest text;

CREATE OR REPLACE FUNCTION aios.semantic_integrity_claim_current(p_claim uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS (
   SELECT 1 FROM aios.claim_candidate cc
   JOIN aios.claim_semantic_integrity si ON si.claim_id=cc.claim_id
   JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
   JOIN aios.document_section ds ON ds.section_id=es.section_id
   WHERE cc.claim_id=p_claim AND si.status='valid'
     AND si.source_text=cc.raw_text
     AND si.validator_version IN (
       'semantic-integrity-v3-fidelity','semantic-integrity-v4-source-coverage')
     AND (si.source_section_digest IS NULL OR
          si.source_section_digest=encode(aios.digest(convert_to(COALESCE(ds.content,''),'UTF8'),'sha256'),'hex'))
     AND jsonb_typeof(si.frame_snapshot)='array'
     AND jsonb_array_length(si.frame_snapshot)>0
     AND jsonb_array_length(si.frame_snapshot)=(
         SELECT COUNT(*) FROM aios.claim_semantic_frame f
         WHERE f.claim_id=cc.claim_id AND f.decomposer_version='semantic-frame-v2')
     AND NOT EXISTS (
       SELECT 1 FROM aios.claim_semantic_frame f
       WHERE f.claim_id=cc.claim_id AND f.decomposer_version='semantic-frame-v2'
         AND NOT EXISTS (
           SELECT 1 FROM jsonb_array_elements(si.frame_snapshot) AS saved(value)
           WHERE saved.value->>'frame_id'=f.frame_id::text
             AND (saved.value->>'subject') IS NOT DISTINCT FROM
                 COALESCE(f.resolved_subject,f.subject_text)
             AND (saved.value->>'predicate') IS NOT DISTINCT FROM
                 COALESCE(f.predicate_canonical,f.predicate_surface)
             AND (saved.value->>'object') IS NOT DISTINCT FROM
                 COALESCE(f.resolved_object,f.object_text)
             AND saved.value->>'polarity'=f.polarity::text
             AND saved.value->>'modality'=f.modality
         )
     )
 )
$$;

-- A shared source gate for all downstream consumers. Claimless canonical
-- acquisitions retain their separate, explicit authority requirements.
CREATE OR REPLACE FUNCTION aios.semantic_acquisition_source_eligible(p_acquisition uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM aios.knowledge_acquisition_event kae
    WHERE kae.acquisition_id=p_acquisition
      AND (kae.claim_id IS NULL OR
           (aios.semantic_integrity_claim_current(kae.claim_id)
            AND aios.semantic_occurrence_topology_eligible(
              kae.claim_id,kae.proposition_id)))
  )
$$;

-- Reapply the operator-gated hygiene exclusions from migration 09; merely
-- being resolved and standalone can no longer bypass a missing/stale receipt.
CREATE OR REPLACE FUNCTION aios.semantic_occurrence_topology_eligible(
 requested_claim uuid,requested_proposition uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS (
   SELECT 1 FROM aios.observation o
   JOIN aios.observation_proposition op ON op.observation_id=o.observation_id
   JOIN aios.claim_semantic_frame f ON f.frame_id=op.frame_id
   JOIN aios.semantic_interpretation si ON si.frame_id=f.frame_id
   WHERE o.claim_id=requested_claim
     AND op.proposition_id=requested_proposition
     AND f.claim_id=o.claim_id AND si.claim_id=o.claim_id
     AND f.resolution_status='resolved' AND si.standalone_semantic
     AND aios.semantic_integrity_claim_current(o.claim_id)
     AND NOT aios.semantic_hygiene_occurrence_suppressed(
         o.claim_id,f.frame_id,op.proposition_id)
     AND NOT aios.semantic_hygiene_occurrence_pending_review(
         o.claim_id,f.frame_id,op.proposition_id)
 )
$$;

-- Tighten admission on every recomputation, but do not replace a prior
-- explicit hygiene suppression with a weaker unresolved integrity disposition.
CREATE OR REPLACE FUNCTION aios.enforce_current_integrity_admission()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_claim uuid;
BEGIN
 IF NEW.status='suppressed' THEN RETURN NEW; END IF;
 SELECT claim_id INTO v_claim FROM aios.knowledge_acquisition_event
 WHERE acquisition_id=NEW.acquisition_id;
 IF v_claim IS NOT NULL AND NOT aios.semantic_integrity_claim_current(v_claim) THEN
   NEW.status:='unresolved';
   NEW.reason:='integrity_receipt_not_current';
   NEW.confidence:=0;
   NEW.meta:=COALESCE(NEW.meta,'{}'::jsonb) ||
     jsonb_build_object('source_integrity_gate','integrity-contract-v1');
 ELSIF v_claim IS NOT NULL AND NOT aios.semantic_acquisition_source_eligible(NEW.acquisition_id) THEN
   NEW.status:='unresolved';
   NEW.reason:='source_occurrence_not_eligible';
   NEW.confidence:=0;
 END IF;
 RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_zzz_current_integrity_admission
 ON aios.semantic_evidence_admission;
CREATE TRIGGER trg_zzz_current_integrity_admission
BEFORE INSERT OR UPDATE ON aios.semantic_evidence_admission
FOR EACH ROW EXECUTE FUNCTION aios.enforce_current_integrity_admission();

-- Invalidation after a validator receipt changes is bounded by one claim.
-- Existing admission status triggers enqueue all descendant beliefs as needed.
CREATE OR REPLACE FUNCTION aios.refresh_admission_from_integrity_receipt()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
 FOR r IN SELECT acquisition_id FROM aios.knowledge_acquisition_event
          WHERE claim_id=NEW.claim_id
 LOOP
   PERFORM aios.recompute_semantic_evidence_admission(r.acquisition_id);
 END LOOP;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_refresh_admission_from_integrity_receipt
 ON aios.claim_semantic_integrity;
CREATE TRIGGER trg_refresh_admission_from_integrity_receipt
AFTER INSERT OR UPDATE OF revision_key,validator_version,status,source_text,
  frame_snapshot,source_section_digest ON aios.claim_semantic_integrity
FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_integrity_receipt();

CREATE OR REPLACE FUNCTION aios.refresh_admission_from_claim_source_edit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
 IF OLD.raw_text IS NOT DISTINCT FROM NEW.raw_text THEN RETURN NEW; END IF;
 FOR r IN SELECT acquisition_id FROM aios.knowledge_acquisition_event
          WHERE claim_id=NEW.claim_id
 LOOP
   PERFORM aios.recompute_semantic_evidence_admission(r.acquisition_id);
 END LOOP;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_refresh_admission_from_claim_source_edit
 ON aios.claim_candidate;
CREATE TRIGGER trg_refresh_admission_from_claim_source_edit
AFTER UPDATE OF raw_text ON aios.claim_candidate
FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_claim_source_edit();


-- Authoritative base family-policy implementation, with lineage and authority
-- eligibility aligned to the newer V3 materializer.
CREATE OR REPLACE FUNCTION aios.apply_character_belief_policy(p_instance_id uuid, p_atom_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_character_id text;
    v_scope_key text;
    v_family text := 'UNKNOWN';
    v_policy_name text := 'generic';
    v_policy_mode text := 'accumulate';
    v_accept_support double precision := 0.60;
    v_decision_margin double precision := 0.15;
    v_exclusive_slot boolean := false;
    v_positive double precision;
    v_negative double precision;
    v_confidence double precision;
    v_stance text;
    v_preferred_proposition_id uuid;
    v_preferred_evidence_instance_id uuid;
    v_resolved_through_node_id uuid;
    v_winner_atom_id uuid;
    v_subject_norm text;
    v_independent_count integer := 0;
BEGIN
    SELECT ci.character_id
    INTO v_character_id
    FROM aios.character_instance ci
    WHERE ci.instance_id=p_instance_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    v_scope_key := 'char:' || v_character_id;

    WITH lineage AS (
        SELECT instance_id,depth FROM aios.cognitive_evidence_instances(p_instance_id)
    )
    SELECT COALESCE(ccr.predicate_family, 'UNKNOWN')
    INTO v_family
    FROM lineage
    JOIN aios.character_proposition_knowledge cpk
      ON cpk.instance_id=lineage.instance_id
    JOIN aios.proposition p ON p.proposition_id=cpk.proposition_id
    JOIN aios.knowledge_acquisition_event kae
      ON kae.instance_id=cpk.instance_id
     AND kae.proposition_id=cpk.proposition_id
    JOIN aios.semantic_evidence_admission sea
      ON sea.acquisition_id=kae.acquisition_id
     AND sea.status='active'
     JOIN aios.epistemic_authority_admission eaa
       ON eaa.acquisition_id=kae.acquisition_id
      AND 'belief'=ANY(eaa.authorized_uses)
      AND (kae.claim_id IS NULL OR aios.semantic_integrity_claim_current(kae.claim_id))
    LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=kae.claim_id
    LEFT JOIN aios.dag_node dn ON dn.node_id=kae.dag_node_id
    LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
    WHERE p.atom_id=p_atom_id
      AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
    ORDER BY COALESCE(dn.created_at, kae.created_at) DESC, kae.created_at DESC
    LIMIT 1;

    v_family := COALESCE(v_family, 'UNKNOWN');

    SELECT policy_name, policy_mode, accept_support, decision_margin, exclusive_slot
    INTO v_policy_name, v_policy_mode, v_accept_support, v_decision_margin, v_exclusive_slot
    FROM aios.reconciliation_family_policy
    WHERE predicate_family=v_family;

    IF NOT FOUND THEN
        SELECT policy_name, policy_mode, accept_support, decision_margin, exclusive_slot
        INTO v_policy_name, v_policy_mode, v_accept_support, v_decision_margin, v_exclusive_slot
        FROM aios.reconciliation_family_policy
        WHERE predicate_family='UNKNOWN';
    END IF;

    IF NOT FOUND OR v_accept_support IS NULL OR v_decision_margin IS NULL
       OR v_policy_mode IS NULL THEN
        RAISE EXCEPTION 'Missing family reconciliation policy for % and UNKNOWN fallback',v_family;
    END IF;

    -- Location is an exclusive current-state slot. Historical evidence remains
    -- untouched, but only the newest admitted location atom may remain active.
    IF v_exclusive_slot THEN
        SELECT a.subject_norm INTO v_subject_norm
        FROM aios.semantic_atom a
        WHERE a.atom_id=p_atom_id;

        WITH RECURSIVE lineage AS (
            SELECT ci.instance_id, ci.parent_instance_id
            FROM aios.character_instance ci
            WHERE ci.instance_id=p_instance_id
            UNION ALL
            SELECT parent.instance_id, parent.parent_instance_id
            FROM lineage
            JOIN aios.character_instance parent
              ON parent.instance_id=lineage.parent_instance_id
        )
        SELECT p.atom_id
        INTO v_winner_atom_id
        FROM lineage
        JOIN aios.character_proposition_knowledge cpk
          ON cpk.instance_id=lineage.instance_id
        JOIN aios.proposition p ON p.proposition_id=cpk.proposition_id
        JOIN aios.semantic_atom a ON a.atom_id=p.atom_id
        JOIN aios.knowledge_acquisition_event kae
          ON kae.instance_id=cpk.instance_id
         AND kae.proposition_id=cpk.proposition_id
        JOIN aios.semantic_evidence_admission sea
          ON sea.acquisition_id=kae.acquisition_id
         AND sea.status='active'
     JOIN aios.epistemic_authority_admission eaa
       ON eaa.acquisition_id=kae.acquisition_id
      AND 'belief'=ANY(eaa.authorized_uses)
      AND (kae.claim_id IS NULL OR aios.semantic_integrity_claim_current(kae.claim_id))
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=kae.claim_id
        LEFT JOIN aios.dag_node dn ON dn.node_id=kae.dag_node_id
        LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE ccr.predicate_family=v_family
          AND a.subject_norm IS NOT DISTINCT FROM v_subject_norm
          AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
        ORDER BY COALESCE(dn.created_at, kae.created_at) DESC, kae.created_at DESC, kae.acquisition_id DESC
        LIMIT 1;

        IF v_winner_atom_id IS NOT NULL AND v_winner_atom_id <> p_atom_id THEN
            UPDATE aios.character_belief_state
            SET stance='unresolved',
                positive_support=0.0,
                negative_support=0.0,
                belief_confidence=0.0,
                resolver_version='character-belief-v4-authority-family',
                meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                    'policy_engine_version','semantic-policy-v1',
                    'predicate_family',v_family,
                    'policy_name',v_policy_name,
                    'policy_mode',v_policy_mode,
                    'exclusive_slot',true,
                    'slot_winner_atom_id',v_winner_atom_id,
                    'state','historical_not_current'
                ),
                updated_at=now()
            WHERE instance_id=p_instance_id AND atom_id=p_atom_id;

            UPDATE aios.semantic_topology_node
            SET significance=0.0,
                meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                    'stance','unresolved',
                    'belief_confidence',0.0,
                    'policy_engine_version','semantic-policy-v1',
                    'policy_name',v_policy_name,
                    'state','historical_not_current'
                ),
                updated_at=now()
            WHERE scope_key=v_scope_key
              AND node_type='BELIEF_STATE'
              AND node_key='belief:' || p_instance_id::text || ':' || p_atom_id::text;
            RETURN;
        END IF;
    END IF;

    IF v_policy_mode IN ('latest','max') THEN
        WITH lineage AS (
            SELECT instance_id,depth FROM aios.cognitive_evidence_instances(p_instance_id)
        ),
        evidence AS (
            SELECT
                kae.acquisition_id,
                cpk.instance_id AS evidence_instance_id,
                cpk.proposition_id,
                p.polarity,
                LEAST(0.999999, GREATEST(0.0,
                    COALESCE(cpk.effective_confidence, cpk.confidence, 0.5)
                    * COALESCE(sea.confidence, 1.0)
                )) AS weight,
                COALESCE(kae.dag_node_id, cpk.last_node_id) AS evidence_node_id,
                COALESCE(dn.created_at, kae.created_at) AS evidence_time,
                COALESCE(
                    kae.meta->>'source_key',
                    cpk.meta->>'source_key',
                    kae.source_entity_id::text,
                    'unknown'
                ) || ':' || COALESCE(
                    kae.dag_node_id::text,
                    cpk.last_node_id::text,
                    kae.acquisition_id::text
                ) AS correlation_key,
                lineage.depth
            FROM lineage
            JOIN aios.character_proposition_knowledge cpk
              ON cpk.instance_id=lineage.instance_id
            JOIN aios.proposition p ON p.proposition_id=cpk.proposition_id
            JOIN aios.knowledge_acquisition_event kae
              ON kae.instance_id=cpk.instance_id
             AND kae.proposition_id=cpk.proposition_id
            JOIN aios.semantic_evidence_admission sea
              ON sea.acquisition_id=kae.acquisition_id
             AND sea.status='active'
     JOIN aios.epistemic_authority_admission eaa
       ON eaa.acquisition_id=kae.acquisition_id
      AND 'belief'=ANY(eaa.authorized_uses)
      AND (kae.claim_id IS NULL OR aios.semantic_integrity_claim_current(kae.claim_id))
            LEFT JOIN aios.dag_node dn ON dn.node_id=kae.dag_node_id
            LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
            WHERE p.atom_id=p_atom_id
              AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
        ),
        correlated AS (
            SELECT DISTINCT ON (polarity, correlation_key)
                polarity, correlation_key, weight, evidence_time,
                proposition_id, evidence_instance_id, evidence_node_id, depth
            FROM evidence
            ORDER BY polarity, correlation_key, weight DESC, evidence_time DESC
        ),
        selected AS (
            SELECT *
            FROM correlated
            WHERE v_policy_mode='max'
               OR (polarity, correlation_key) = (
                    SELECT c2.polarity, c2.correlation_key
                    FROM correlated c2
                    ORDER BY c2.evidence_time DESC, c2.weight DESC, c2.correlation_key
                    LIMIT 1
               )
        )
        SELECT
            CASE
                WHEN v_policy_mode='max' THEN COALESCE(MAX(weight) FILTER (WHERE polarity=1),0.0)
                ELSE COALESCE(MAX(weight) FILTER (WHERE polarity=1),0.0)
            END,
            CASE
                WHEN v_policy_mode='max' THEN COALESCE(MAX(weight) FILTER (WHERE polarity=-1),0.0)
                ELSE COALESCE(MAX(weight) FILTER (WHERE polarity=-1),0.0)
            END,
            COUNT(*),
            (ARRAY_AGG(proposition_id ORDER BY
                CASE WHEN v_policy_mode='latest' THEN evidence_time END DESC,
                weight DESC, depth ASC, proposition_id
            ))[1],
            (ARRAY_AGG(evidence_instance_id ORDER BY
                CASE WHEN v_policy_mode='latest' THEN evidence_time END DESC,
                weight DESC, depth ASC, proposition_id
            ))[1],
            (ARRAY_AGG(evidence_node_id ORDER BY
                CASE WHEN v_policy_mode='latest' THEN evidence_time END DESC,
                weight DESC, depth ASC, proposition_id
            ))[1]
        INTO v_positive, v_negative, v_independent_count,
             v_preferred_proposition_id, v_preferred_evidence_instance_id,
             v_resolved_through_node_id
        FROM selected;

        v_positive := COALESCE(v_positive, 0.0);
        v_negative := COALESCE(v_negative, 0.0);
        IF v_positive >= v_accept_support AND v_positive - v_negative >= v_decision_margin THEN
            v_stance := 'positive';
        ELSIF v_negative >= v_accept_support AND v_negative - v_positive >= v_decision_margin THEN
            v_stance := 'negative';
        ELSE
            v_stance := 'unresolved';
        END IF;
        v_confidence := LEAST(1.0, GREATEST(0.0, abs(v_positive-v_negative)));

        UPDATE aios.character_belief_state
        SET stance=v_stance,
            positive_support=v_positive,
            negative_support=v_negative,
            belief_confidence=v_confidence,
            preferred_proposition_id=COALESCE(v_preferred_proposition_id, preferred_proposition_id),
            preferred_evidence_instance_id=COALESCE(v_preferred_evidence_instance_id, preferred_evidence_instance_id),
            independent_evidence_count=v_independent_count,
            resolved_through_node_id=COALESCE(v_resolved_through_node_id, resolved_through_node_id),
            resolver_version='character-belief-v4-authority-family',
            meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                'policy_engine_version','semantic-policy-v1',
                'predicate_family',v_family,
                'policy_name',v_policy_name,
                'policy_mode',v_policy_mode,
                'accept_support',v_accept_support,
                'decision_margin',v_decision_margin,
                'exclusive_slot',v_exclusive_slot
            ),
            updated_at=now()
        WHERE instance_id=p_instance_id AND atom_id=p_atom_id;

        UPDATE aios.semantic_topology_node
        SET proposition_id=COALESCE(v_preferred_proposition_id, proposition_id),
            dag_node_id=COALESCE(v_resolved_through_node_id, dag_node_id),
            significance=v_confidence,
            meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                'stance',v_stance,
                'positive_support',v_positive,
                'negative_support',v_negative,
                'belief_confidence',v_confidence,
                'policy_engine_version','semantic-policy-v1',
                'predicate_family',v_family,
                'policy_name',v_policy_name,
                'policy_mode',v_policy_mode,
                'resolver_version','character-belief-v4-authority-family'
            ),
            updated_at=now()
        WHERE scope_key=v_scope_key
          AND node_type='BELIEF_STATE'
          AND node_key='belief:' || p_instance_id::text || ':' || p_atom_id::text;

        UPDATE aios.semantic_topology_edge
        SET significance=v_confidence,
            meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                'stance',v_stance,
                'policy_engine_version','semantic-policy-v1',
                'policy_name',v_policy_name,
                'resolver_version','character-belief-v4-authority-family'
            )
        WHERE scope_key=v_scope_key
          AND edge_type='holds_belief_state'
          AND child_node_id=(
              SELECT topology_node_id
              FROM aios.semantic_topology_node
              WHERE scope_key=v_scope_key
                AND node_type='BELIEF_STATE'
                AND node_key='belief:' || p_instance_id::text || ':' || p_atom_id::text
          );
    ELSE
        UPDATE aios.character_belief_state
        SET resolver_version='character-belief-v4-authority-family',
            meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                'policy_engine_version','semantic-policy-v1',
                'predicate_family',v_family,
                'policy_name',v_policy_name,
                'policy_mode',v_policy_mode,
                'accept_support',v_accept_support,
                'decision_margin',v_decision_margin,
                'exclusive_slot',v_exclusive_slot
            ),
            updated_at=now()
        WHERE instance_id=p_instance_id AND atom_id=p_atom_id;
    END IF;
END;
$$;

-- Extracted authority-lineage implementation with fail-closed default lookup.
CREATE OR REPLACE FUNCTION aios.reconcile_character_belief_atom_authority_v3(
    p_instance_id uuid,
    p_atom_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_character_id text;
    v_world_id uuid;
    v_scope_key text;
    v_accept_support double precision;
    v_decision_margin double precision;
    v_positive_support double precision := 0.0;
    v_negative_support double precision := 0.0;
    v_evidence_count integer := 0;
    v_independent_count integer := 0;
    v_stance text;
    v_belief_confidence double precision := 0.0;
    v_preferred_proposition_id uuid;
    v_preferred_evidence_instance_id uuid;
    v_resolved_through_node_id uuid;
    v_root_node_id uuid;
    v_instance_node_id uuid;
    v_belief_node_id uuid;
    v_label text;
BEGIN
    SELECT ci.character_id, COALESCE(ci.current_world_id, ci.world_id)
    INTO v_character_id, v_world_id
    FROM aios.character_instance ci
    WHERE ci.instance_id=p_instance_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    v_scope_key := 'char:' || v_character_id;

    SELECT accept_support, decision_margin
    INTO STRICT v_accept_support, v_decision_margin
    FROM aios.belief_reconciliation_policy
    WHERE policy_key='default';

    IF v_accept_support IS NULL OR v_decision_margin IS NULL THEN
        RAISE EXCEPTION 'Default belief reconciliation policy has null thresholds';
    END IF;

    WITH lineage AS (
        SELECT instance_id, depth
        FROM aios.cognitive_evidence_instances(p_instance_id)
    ),
    raw_evidence AS (
        -- Keep each acquisition here. The next CTE deliberately collapses only
        -- acquisitions that share the same source/event coordinate, so truly
        -- independent corroboration can strengthen belief without counting
        -- duplicate extraction artifacts as separate witnesses.
        SELECT
            cpk.instance_id AS evidence_instance_id,
            cpk.proposition_id,
            p.polarity,
            LEAST(
                0.999999,
                GREATEST(
                    0.0,
                    COALESCE(cpk.effective_confidence, cpk.confidence, 0.5)
                    * COALESCE(sea.confidence, 1.0)
                )
            ) AS weight,
            COALESCE(kae.dag_node_id, cpk.last_node_id) AS evidence_node_id,
            COALESCE(eaa.lineage_key, aios.evidence_correlation_key(
                ie.source,
                ie.source_kind,
                ie.source_event_id,
                ie.message_text,
                ie.payload,
                ie.speaker_role::text,
                COALESCE(ie.character_id, v_character_id),
                COALESCE(
                    kae.meta->>'source_key',
                    cpk.meta->>'source_key',
                    kae.source_entity_id::text,
                    'unknown'
                ),
                COALESCE(
                    kae.dag_node_id::text,
                    cpk.last_node_id::text,
                    kae.acquisition_id::text
                )
            )) AS correlation_key,
            lineage.depth
        FROM lineage
        JOIN aios.character_proposition_knowledge cpk
          ON cpk.instance_id=lineage.instance_id
        JOIN aios.proposition p
          ON p.proposition_id=cpk.proposition_id
        JOIN aios.knowledge_acquisition_event kae
          ON kae.instance_id=cpk.instance_id
         AND kae.proposition_id=cpk.proposition_id
        JOIN aios.semantic_evidence_admission sea
          ON sea.acquisition_id=kae.acquisition_id
         AND sea.status='active'
        JOIN aios.epistemic_authority_admission eaa
          ON eaa.acquisition_id=kae.acquisition_id
         AND 'belief'=ANY(eaa.authorized_uses)
          AND (kae.claim_id IS NULL OR aios.semantic_integrity_claim_current(kae.claim_id))
        LEFT JOIN aios.dag_node dn
          ON dn.node_id=kae.dag_node_id
        LEFT JOIN aios.ingest_event ie
          ON ie.event_id=dn.event_id
        WHERE p.atom_id=p_atom_id
          AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
    ),
    correlated AS (
        SELECT
            polarity,
            correlation_key,
            MAX(weight) AS weight
        FROM raw_evidence
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
        (SELECT COUNT(*) FROM raw_evidence),
        COUNT(*)
    INTO
        v_positive_support,
        v_negative_support,
        v_evidence_count,
        v_independent_count
    FROM correlated;

    IF v_evidence_count = 0 THEN
        DELETE FROM aios.character_belief_state
        WHERE instance_id=p_instance_id AND atom_id=p_atom_id;

        DELETE FROM aios.semantic_topology_node
        WHERE scope_key=v_scope_key
          AND node_type='BELIEF_STATE'
          AND node_key='belief:' || p_instance_id::text || ':' || p_atom_id::text;
        RETURN;
    END IF;

    IF v_positive_support >= v_accept_support
       AND (v_positive_support - v_negative_support) >= v_decision_margin THEN
        v_stance := 'positive';
    ELSIF v_negative_support >= v_accept_support
       AND (v_negative_support - v_positive_support) >= v_decision_margin THEN
        v_stance := 'negative';
    ELSE
        v_stance := 'unresolved';
    END IF;

    v_belief_confidence := LEAST(
        1.0,
        GREATEST(0.0, abs(v_positive_support - v_negative_support))
    );

    WITH lineage AS (
        SELECT instance_id, depth
        FROM aios.cognitive_evidence_instances(p_instance_id)
    ),
    raw_evidence AS (
        SELECT DISTINCT ON (cpk.instance_id, cpk.proposition_id)
            cpk.instance_id AS evidence_instance_id,
            cpk.proposition_id,
            p.polarity,
            LEAST(
                0.999999,
                GREATEST(
                    0.0,
                    COALESCE(cpk.effective_confidence, cpk.confidence, 0.5)
                    * COALESCE(sea.confidence, 1.0)
                )
            ) AS weight,
            COALESCE(kae.dag_node_id, cpk.last_node_id) AS evidence_node_id,
            lineage.depth
        FROM lineage
        JOIN aios.character_proposition_knowledge cpk
          ON cpk.instance_id=lineage.instance_id
        JOIN aios.proposition p
          ON p.proposition_id=cpk.proposition_id
        JOIN aios.knowledge_acquisition_event kae
          ON kae.instance_id=cpk.instance_id
         AND kae.proposition_id=cpk.proposition_id
        JOIN aios.semantic_evidence_admission sea
          ON sea.acquisition_id=kae.acquisition_id
         AND sea.status='active'
        JOIN aios.epistemic_authority_admission eaa
          ON eaa.acquisition_id=kae.acquisition_id
         AND 'belief'=ANY(eaa.authorized_uses)
          AND (kae.claim_id IS NULL OR aios.semantic_integrity_claim_current(kae.claim_id))
        LEFT JOIN aios.dag_node dn
          ON dn.node_id=kae.dag_node_id
        LEFT JOIN aios.ingest_event ie
          ON ie.event_id=dn.event_id
        WHERE p.atom_id=p_atom_id
          AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
        ORDER BY
            cpk.instance_id,
            cpk.proposition_id,
            kae.created_at DESC,
            kae.acquisition_id DESC
    )
    SELECT
        proposition_id,
        evidence_instance_id,
        evidence_node_id
    INTO
        v_preferred_proposition_id,
        v_preferred_evidence_instance_id,
        v_resolved_through_node_id
    FROM raw_evidence
    ORDER BY
        CASE
            WHEN v_stance='positive' AND polarity=1 THEN 0
            WHEN v_stance='negative' AND polarity=-1 THEN 0
            WHEN v_stance='unresolved' THEN 0
            ELSE 1
        END,
        weight DESC,
        depth ASC,
        proposition_id
    LIMIT 1;

    INSERT INTO aios.character_belief_state (
        instance_id, atom_id, stance,
        positive_support, negative_support, belief_confidence,
        preferred_proposition_id, preferred_evidence_instance_id,
        evidence_count, independent_evidence_count,
        resolved_through_node_id, resolver_version, meta,
        resolved_at, updated_at
    )
    VALUES (
        p_instance_id,
        p_atom_id,
        v_stance,
        v_positive_support,
        v_negative_support,
        v_belief_confidence,
        v_preferred_proposition_id,
        v_preferred_evidence_instance_id,
        v_evidence_count,
        v_independent_count,
        v_resolved_through_node_id,
        'character-belief-v3-authority-lineage',
        jsonb_build_object(
            'evidence_topology_preserved', true,
            'accept_support', v_accept_support,
            'decision_margin', v_decision_margin
        ),
        now(),
        now()
    )
    ON CONFLICT (instance_id, atom_id) DO UPDATE
    SET stance=EXCLUDED.stance,
        positive_support=EXCLUDED.positive_support,
        negative_support=EXCLUDED.negative_support,
        belief_confidence=EXCLUDED.belief_confidence,
        preferred_proposition_id=EXCLUDED.preferred_proposition_id,
        preferred_evidence_instance_id=EXCLUDED.preferred_evidence_instance_id,
        evidence_count=EXCLUDED.evidence_count,
        independent_evidence_count=EXCLUDED.independent_evidence_count,
        resolved_through_node_id=EXCLUDED.resolved_through_node_id,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,
        resolved_at=now(),
        updated_at=now();

    SELECT p.canonical_text
    INTO v_label
    FROM aios.proposition p
    WHERE p.proposition_id=v_preferred_proposition_id;

    INSERT INTO aios.semantic_topology_node (
        scope_key, scope_kind, node_type, node_key, label,
        character_id, character_instance_id, world_id,
        significance, meta
    )
    VALUES (
        v_scope_key, 'character', 'ROOT', 'root', v_scope_key,
        v_character_id, NULL, v_world_id,
        1.0, jsonb_build_object('belief_resolver_version', 'character-belief-v3-authority-lineage')
    )
    ON CONFLICT (scope_key, node_type, node_key) DO UPDATE
    SET updated_at=now()
    RETURNING topology_node_id INTO v_root_node_id;

    INSERT INTO aios.semantic_topology_node (
        scope_key, scope_kind, node_type, node_key, label,
        character_id, character_instance_id, world_id,
        significance, meta
    )
    VALUES (
        v_scope_key, 'character', 'INSTANCE', p_instance_id::text,
        'character-instance:' || p_instance_id::text,
        v_character_id, p_instance_id, v_world_id,
        1.0, jsonb_build_object('belief_resolver_version', 'character-belief-v3-authority-lineage')
    )
    ON CONFLICT (scope_key, node_type, node_key) DO UPDATE
    SET character_instance_id=EXCLUDED.character_instance_id,
        world_id=COALESCE(EXCLUDED.world_id, aios.semantic_topology_node.world_id),
        updated_at=now()
    RETURNING topology_node_id INTO v_instance_node_id;

    INSERT INTO aios.semantic_topology_edge (
        scope_key, parent_node_id, child_node_id, edge_type,
        significance, meta
    )
    VALUES (
        v_scope_key, v_root_node_id, v_instance_node_id,
        'experiential_branch', 1.0,
        jsonb_build_object('belief_resolver_version', 'character-belief-v3-authority-lineage')
    )
    ON CONFLICT (scope_key, parent_node_id, child_node_id, edge_type) DO UPDATE
    SET significance=GREATEST(aios.semantic_topology_edge.significance, EXCLUDED.significance),
        meta=aios.semantic_topology_edge.meta || EXCLUDED.meta;

    INSERT INTO aios.semantic_topology_node (
        scope_key, scope_kind, node_type, node_key, label,
        character_id, character_instance_id, world_id,
        dag_node_id, proposition_id, significance, meta
    )
    VALUES (
        v_scope_key,
        'character',
        'BELIEF_STATE',
        'belief:' || p_instance_id::text || ':' || p_atom_id::text,
        v_label,
        v_character_id,
        p_instance_id,
        v_world_id,
        v_resolved_through_node_id,
        v_preferred_proposition_id,
        LEAST(1.0, GREATEST(0.0, v_belief_confidence)),
        jsonb_build_object(
            'atom_id', p_atom_id,
            'stance', v_stance,
            'positive_support', v_positive_support,
            'negative_support', v_negative_support,
            'belief_confidence', v_belief_confidence,
            'evidence_count', v_evidence_count,
            'independent_evidence_count', v_independent_count,
            'resolver_version', 'character-belief-v3-authority-lineage'
        )
    )
    ON CONFLICT (scope_key, node_type, node_key) DO UPDATE
    SET label=EXCLUDED.label,
        character_instance_id=EXCLUDED.character_instance_id,
        world_id=EXCLUDED.world_id,
        dag_node_id=EXCLUDED.dag_node_id,
        proposition_id=EXCLUDED.proposition_id,
        significance=EXCLUDED.significance,
        meta=EXCLUDED.meta,
        updated_at=now()
    RETURNING topology_node_id INTO v_belief_node_id;

    INSERT INTO aios.semantic_topology_edge (
        scope_key, parent_node_id, child_node_id, edge_type,
        significance, meta
    )
    VALUES (
        v_scope_key,
        v_instance_node_id,
        v_belief_node_id,
        'holds_belief_state',
        LEAST(1.0, GREATEST(0.0, v_belief_confidence)),
        jsonb_build_object(
            'stance', v_stance,
            'resolver_version', 'character-belief-v3-authority-lineage'
        )
    )
    ON CONFLICT (scope_key, parent_node_id, child_node_id, edge_type) DO UPDATE
    SET significance=EXCLUDED.significance,
        meta=EXCLUDED.meta;

    DELETE FROM aios.semantic_topology_edge
    WHERE scope_key=v_scope_key
      AND parent_node_id=v_belief_node_id
      AND edge_type IN ('supports_belief_atom','opposes_belief_atom');

    WITH lineage AS (
        SELECT instance_id, depth
        FROM aios.cognitive_evidence_instances(p_instance_id)
    ),
    evidence AS (
        SELECT DISTINCT ON (kae.acquisition_id)
            kae.acquisition_id,
            p.polarity,
            LEAST(
                1.0,
                GREATEST(
                    0.0,
                    COALESCE(cpk.effective_confidence, cpk.confidence, 0.5)
                    * COALESCE(sea.confidence, 1.0)
                )
            ) AS weight
        FROM lineage
        JOIN aios.character_proposition_knowledge cpk
          ON cpk.instance_id=lineage.instance_id
        JOIN aios.proposition p
          ON p.proposition_id=cpk.proposition_id
        JOIN aios.knowledge_acquisition_event kae
          ON kae.instance_id=cpk.instance_id
         AND kae.proposition_id=cpk.proposition_id
        JOIN aios.semantic_evidence_admission sea
          ON sea.acquisition_id=kae.acquisition_id
         AND sea.status='active'
        JOIN aios.epistemic_authority_admission eaa
          ON eaa.acquisition_id=kae.acquisition_id
         AND 'belief'=ANY(eaa.authorized_uses)
          AND (kae.claim_id IS NULL OR aios.semantic_integrity_claim_current(kae.claim_id))
        LEFT JOIN aios.dag_node dn ON dn.node_id=kae.dag_node_id
        LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
        WHERE p.atom_id=p_atom_id
          AND (kae.claim_id IS NULL OR ie.superseded_at IS NULL)
        ORDER BY kae.acquisition_id, kae.created_at DESC
    )
    INSERT INTO aios.semantic_topology_edge (
        scope_key, parent_node_id, child_node_id, edge_type,
        significance, acquisition_id, meta
    )
    SELECT
        v_scope_key,
        v_belief_node_id,
        n.topology_node_id,
        CASE WHEN evidence.polarity=1
             THEN 'supports_belief_atom'
             ELSE 'opposes_belief_atom'
        END,
        evidence.weight,
        evidence.acquisition_id,
        jsonb_build_object(
            'polarity', evidence.polarity,
            'resolver_version', 'character-belief-v3-authority-lineage'
        )
    FROM evidence
    JOIN aios.semantic_topology_node n
      ON n.scope_key=v_scope_key
     AND n.node_type='EPISTEMIC_TRANSITION'
     AND n.acquisition_id=evidence.acquisition_id
    ON CONFLICT (scope_key, parent_node_id, child_node_id, edge_type) DO UPDATE
    SET significance=EXCLUDED.significance,
        acquisition_id=EXCLUDED.acquisition_id,
        meta=EXCLUDED.meta;
END;
$$;

-- V3's authority-lineage aggregator is retained as the only accumulator.
-- Family policies are applied only AFTER it, inside the same advisory-locked
-- serialized materializer used by live and background reconciliation.
CREATE OR REPLACE FUNCTION aios.reconcile_character_belief_atom(
 p_instance_id uuid,p_atom_id uuid
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 PERFORM aios.assert_belief_policy_configuration();
 PERFORM aios.reconcile_character_belief_atom_authority_v3(p_instance_id,p_atom_id);
 PERFORM aios.apply_character_belief_policy(p_instance_id,p_atom_id);
END $$;

-- This migration deliberately does not recalculate existing records. A
-- scoped operator can enqueue a bounded sample after taking a baseline and
-- completing Experiment 7. Never mass-update stance from support columns.
CREATE OR REPLACE FUNCTION aios.queue_belief_policy_reconciliation(
 p_instance_id uuid,p_limit integer DEFAULT 100
) RETURNS integer LANGUAGE plpgsql AS $$
DECLARE v_count integer;
BEGIN
 IF p_instance_id IS NULL THEN
    RAISE EXCEPTION 'An explicit instance_id is required for policy backfill';
 END IF;
 PERFORM aios.assert_belief_policy_configuration();
 WITH batch AS (
   SELECT bs.instance_id,bs.atom_id
   FROM aios.character_belief_state bs
   WHERE bs.instance_id=p_instance_id
     AND (bs.resolver_version IS DISTINCT FROM 'character-belief-v4-authority-family'
          OR bs.meta->>'policy_engine_version' IS DISTINCT FROM 'semantic-policy-v1')
   ORDER BY bs.atom_id LIMIT GREATEST(1,LEAST(COALESCE(p_limit,100),128))
 )
 INSERT INTO aios.character_belief_reconciliation_dirty
 (instance_id,atom_id,dirty_version,dirty_at)
 SELECT instance_id,atom_id,1,now() FROM batch
 ON CONFLICT(instance_id,atom_id) DO UPDATE
 SET dirty_version=aios.character_belief_reconciliation_dirty.dirty_version+1,
     dirty_at=now();
 GET DIAGNOSTICS v_count=ROW_COUNT;
 RETURN v_count;
END $$;
COMMIT;
