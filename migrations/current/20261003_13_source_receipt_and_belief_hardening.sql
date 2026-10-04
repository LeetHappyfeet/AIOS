-- Additive pre-reset source integrity and belief hardening. No automatic
-- reconciliation or modification to previously persisted evidence.
BEGIN;
ALTER TABLE aios.claim_semantic_integrity
 ADD COLUMN IF NOT EXISTS source_section_id uuid,
 ADD COLUMN IF NOT EXISTS source_sentence_digest text,
 ADD COLUMN IF NOT EXISTS source_node_id uuid;

-- Guard critical default reference data against accidental runtime edits.
CREATE OR REPLACE FUNCTION aios.guard_default_belief_policy()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   IF OLD.policy_key='default' THEN
     RAISE EXCEPTION 'Protected belief reconciliation default: use a reviewed policy migration';
   END IF;
   RETURN OLD;
 END IF;
 IF OLD.policy_key='default' AND (
       NEW.policy_key IS DISTINCT FROM OLD.policy_key
       OR NEW.accept_support IS DISTINCT FROM OLD.accept_support
       OR NEW.decision_margin IS DISTINCT FROM OLD.decision_margin
       OR NEW.resolver_version IS DISTINCT FROM OLD.resolver_version) THEN
   RAISE EXCEPTION 'Protected belief reconciliation default: use a reviewed policy migration';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_guard_default_belief_policy ON aios.belief_reconciliation_policy;
CREATE TRIGGER trg_guard_default_belief_policy
BEFORE UPDATE OR DELETE ON aios.belief_reconciliation_policy
FOR EACH ROW EXECUTE FUNCTION aios.guard_default_belief_policy();

-- V4 has mandatory matching source identity and digests. NULL digest
-- compatibility applies to historical V3 only; it never grants V4 authority.
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
     AND (
       (si.validator_version='semantic-integrity-v3-fidelity'
        AND (si.source_section_digest IS NULL OR si.source_section_digest=
             encode(aios.digest(convert_to(COALESCE(ds.content,''),'UTF8'),'sha256'),'hex')))
       OR
       (si.validator_version='semantic-integrity-v4-source-coverage'
        AND si.source_section_id=ds.section_id
        AND si.source_section_digest=
            encode(aios.digest(convert_to(COALESCE(ds.content,''),'UTF8'),'sha256'),'hex')
        AND si.source_sentence_digest=
            encode(aios.digest(convert_to(COALESCE(es.sentence_text,''),'UTF8'),'sha256'),'hex')
        AND si.source_node_id IS NOT DISTINCT FROM ds.node_id)
     )
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

-- Read-side gates must be current even before an invalidation job drains.
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
      AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)
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

        WITH lineage AS (
            SELECT instance_id,depth FROM aios.cognitive_evidence_instances(p_instance_id)
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
      AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)
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
                preferred_proposition_id=NULL,
                preferred_evidence_instance_id=NULL,
                resolved_through_node_id=NULL,
                evidence_count=0,
                independent_evidence_count=0,
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
                proposition_id=NULL,
                dag_node_id=NULL,
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
            UPDATE aios.semantic_topology_edge
            SET significance=0.0,
                meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                    'stance','unresolved', 'belief_confidence',0.0,
                    'state','historical_not_current',
                    'resolver_version','character-belief-v4-authority-family')
            WHERE scope_key=v_scope_key
              AND edge_type='holds_belief_state'
              AND child_node_id=(
                  SELECT topology_node_id FROM aios.semantic_topology_node
                  WHERE scope_key=v_scope_key AND node_type='BELIEF_STATE'
                    AND node_key='belief:' || p_instance_id::text || ':' || p_atom_id::text
              );
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
      AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)
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
            preferred_proposition_id=v_preferred_proposition_id,
            preferred_evidence_instance_id=v_preferred_evidence_instance_id,
            independent_evidence_count=v_independent_count,
            resolved_through_node_id=v_resolved_through_node_id,
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
        SET proposition_id=v_preferred_proposition_id,
            dag_node_id=v_resolved_through_node_id,
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
          AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)
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
          AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)
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
          AND aios.semantic_acquisition_source_eligible(kae.acquisition_id)
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


-- Use the same admission re-evaluator everywhere. Revisit BOTH ends when
-- occurrences, frames, or interpretations are reassigned between claims.
CREATE OR REPLACE FUNCTION aios.refresh_acquisition_admission_for_claim(p_claim uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE rec record;
BEGIN
 IF p_claim IS NULL THEN RETURN; END IF;
 FOR rec IN SELECT acquisition_id FROM aios.knowledge_acquisition_event WHERE claim_id=p_claim
 LOOP
   PERFORM aios.recompute_semantic_evidence_admission(rec.acquisition_id);
 END LOOP;
END $$;

CREATE OR REPLACE FUNCTION aios.revisit_admission_after_occurrence_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_claim uuid; new_claim uuid;
BEGIN
 IF TG_OP IN ('DELETE','UPDATE') THEN
   SELECT claim_id INTO old_claim FROM aios.observation
   WHERE observation_id=OLD.observation_id;
   PERFORM aios.refresh_acquisition_admission_for_claim(old_claim);
 END IF;
 IF TG_OP IN ('INSERT','UPDATE') THEN
   SELECT claim_id INTO new_claim FROM aios.observation
   WHERE observation_id=NEW.observation_id;
   IF TG_OP='INSERT' OR new_claim IS DISTINCT FROM old_claim THEN
     PERFORM aios.refresh_acquisition_admission_for_claim(new_claim);
   END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION aios.revisit_admission_after_interpretation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP IN ('DELETE','UPDATE') THEN
   PERFORM aios.refresh_acquisition_admission_for_claim(OLD.claim_id);
 END IF;
 IF TG_OP IN ('INSERT','UPDATE') THEN
   IF TG_OP='INSERT' OR NEW.claim_id IS DISTINCT FROM OLD.claim_id THEN
     PERFORM aios.refresh_acquisition_admission_for_claim(NEW.claim_id);
   END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION aios.refresh_admission_from_integrity_frame_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP IN ('DELETE','UPDATE') THEN
   PERFORM aios.refresh_acquisition_admission_for_claim(OLD.claim_id);
 END IF;
 IF TG_OP IN ('INSERT','UPDATE') THEN
   IF TG_OP='INSERT' OR NEW.claim_id IS DISTINCT FROM OLD.claim_id THEN
     PERFORM aios.refresh_acquisition_admission_for_claim(NEW.claim_id);
   END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_refresh_admission_integrity_frame_mutation ON aios.claim_semantic_frame;
CREATE TRIGGER trg_refresh_admission_integrity_frame_mutation
AFTER INSERT OR DELETE OR UPDATE OF claim_id,frame_id,frame_index,
 subject_text,object_text,predicate_surface,polarity,modality,
 decomposer_version,resolved_subject,resolved_object,predicate_canonical,resolution_status
ON aios.claim_semantic_frame
FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_integrity_frame_mutation();

CREATE OR REPLACE FUNCTION aios.refresh_admission_from_claim_source_edit()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.raw_text IS DISTINCT FROM NEW.raw_text
    OR OLD.sentence_id IS DISTINCT FROM NEW.sentence_id THEN
   PERFORM aios.refresh_acquisition_admission_for_claim(NEW.claim_id);
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_refresh_admission_from_claim_source_edit ON aios.claim_candidate;
CREATE TRIGGER trg_refresh_admission_from_claim_source_edit
AFTER UPDATE OF raw_text,sentence_id ON aios.claim_candidate
FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_claim_source_edit();

CREATE OR REPLACE FUNCTION aios.refresh_admission_from_sentence_edit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rec record;
BEGIN
 IF OLD.section_id IS NOT DISTINCT FROM NEW.section_id
    AND OLD.sentence_text IS NOT DISTINCT FROM NEW.sentence_text THEN RETURN NEW; END IF;
 FOR rec IN SELECT DISTINCT claim_id FROM aios.claim_candidate
            WHERE sentence_id=NEW.sentence_id
 LOOP
   PERFORM aios.refresh_acquisition_admission_for_claim(rec.claim_id);
 END LOOP;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_refresh_admission_integrity_sentence_edit ON aios.extracted_sentence;
CREATE TRIGGER trg_refresh_admission_integrity_sentence_edit
AFTER UPDATE OF section_id,sentence_text ON aios.extracted_sentence
FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_sentence_edit();

CREATE OR REPLACE FUNCTION aios.refresh_admission_from_section_context_edit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rec record;
BEGIN
 IF OLD.content IS NOT DISTINCT FROM NEW.content
    AND OLD.node_id IS NOT DISTINCT FROM NEW.node_id THEN RETURN NEW; END IF;
 FOR rec IN SELECT DISTINCT cc.claim_id
   FROM aios.extracted_sentence es
   JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
   WHERE es.section_id=NEW.section_id
 LOOP
   PERFORM aios.refresh_acquisition_admission_for_claim(rec.claim_id);
 END LOOP;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_refresh_admission_integrity_section_edit ON aios.document_section;
CREATE TRIGGER trg_refresh_admission_integrity_section_edit
AFTER UPDATE OF content,node_id ON aios.document_section
FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_section_context_edit();

-- Skip no-op writes after hygiene and integrity BEFORE UPDATE gates so
-- repeated trigger firings cannot advance downstream dirty generations.
CREATE OR REPLACE FUNCTION aios.skip_unchanged_semantic_admission()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.status,NEW.reason,NEW.confidence,NEW.resolver_version,NEW.meta)
    IS NOT DISTINCT FROM
    (OLD.status,OLD.reason,OLD.confidence,OLD.resolver_version,OLD.meta) THEN
   RETURN NULL;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_zzzz_skip_unchanged_semantic_admission ON aios.semantic_evidence_admission;
CREATE TRIGGER trg_zzzz_skip_unchanged_semantic_admission
BEFORE UPDATE ON aios.semantic_evidence_admission
FOR EACH ROW EXECUTE FUNCTION aios.skip_unchanged_semantic_admission();

COMMENT ON FUNCTION aios.semantic_integrity_claim_current(uuid) IS
 'V4 requires matching section identity, paragraph digest, extracted sentence digest, and source DAG node. Historical V3 compatibility is explicit and not a V4 upgrade.';
SELECT aios.assert_belief_policy_configuration();
COMMIT;
