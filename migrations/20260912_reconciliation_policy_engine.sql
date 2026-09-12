-- Semantic-family reconciliation policy engine.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.reconciliation_family_policy (
    predicate_family text PRIMARY KEY,
    policy_name text NOT NULL,
    policy_mode text NOT NULL CHECK (policy_mode IN ('accumulate','latest','max')),
    accept_support double precision NOT NULL CHECK (accept_support BETWEEN 0 AND 1),
    decision_margin double precision NOT NULL CHECK (decision_margin BETWEEN 0 AND 1),
    exclusive_slot boolean NOT NULL DEFAULT false,
    resolver_version text NOT NULL DEFAULT 'semantic-policy-v1',
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO aios.reconciliation_family_policy
    (predicate_family, policy_name, policy_mode, accept_support, decision_margin, exclusive_slot)
VALUES
    ('UNKNOWN',       'generic',           'accumulate', 0.60, 0.15, false),
    ('IDENTITY',      'identity',          'accumulate', 0.60, 0.15, false),
    ('SOCIAL',        'relationship',      'accumulate', 0.60, 0.15, false),
    ('MEMBERSHIP',    'membership',        'accumulate', 0.60, 0.15, false),
    ('POSSESSION',    'possession',        'accumulate', 0.60, 0.15, false),
    ('EPISTEMIC',     'epistemic',         'accumulate', 0.55, 0.12, false),
    ('MEMORY',        'memory',            'accumulate', 0.55, 0.12, false),
    ('CAUSAL',        'causal',            'accumulate', 0.65, 0.15, false),
    ('COMMUNICATION', 'communication',     'accumulate', 0.60, 0.15, false),
    ('ACTION',        'event',             'accumulate', 0.60, 0.15, false),
    ('TEMPORAL',      'temporal',          'accumulate', 0.60, 0.15, false),
    ('DESCRIPTIVE',   'descriptive_state', 'latest',     0.50, 0.05, false),
    ('EMOTIONAL',     'emotional_state',   'latest',     0.50, 0.05, false),
    ('GOAL',          'goal_lifecycle',    'latest',     0.50, 0.05, false),
    ('SPATIAL',       'location_state',    'latest',     0.50, 0.05, true),
    ('RULE',          'authority_rule',    'max',        0.70, 0.15, false)
ON CONFLICT (predicate_family) DO UPDATE
SET policy_name=EXCLUDED.policy_name,
    policy_mode=EXCLUDED.policy_mode,
    accept_support=EXCLUDED.accept_support,
    decision_margin=EXCLUDED.decision_margin,
    exclusive_slot=EXCLUDED.exclusive_slot,
    resolver_version='semantic-policy-v1',
    updated_at=now();

CREATE OR REPLACE FUNCTION aios.apply_character_belief_policy(
    p_instance_id uuid,
    p_atom_id uuid
)
RETURNS void
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
                resolver_version='character-belief-policy-v2',
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
        WITH RECURSIVE lineage AS (
            SELECT ci.instance_id, ci.parent_instance_id, 0 AS depth
            FROM aios.character_instance ci
            WHERE ci.instance_id=p_instance_id
            UNION ALL
            SELECT parent.instance_id, parent.parent_instance_id, lineage.depth + 1
            FROM lineage
            JOIN aios.character_instance parent
              ON parent.instance_id=lineage.parent_instance_id
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
            resolver_version='character-belief-policy-v2',
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
                'resolver_version','character-belief-policy-v2'
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
                'resolver_version','character-belief-policy-v2'
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
        SET resolver_version='character-belief-policy-v2',
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

DO $$
BEGIN
    IF to_regprocedure('aios.reconcile_character_belief_atom_generic_v1(uuid,uuid)') IS NULL THEN
        EXECUTE 'ALTER FUNCTION aios.reconcile_character_belief_atom(uuid,uuid) RENAME TO reconcile_character_belief_atom_generic_v1';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION aios.reconcile_character_belief_atom(
    p_instance_id uuid,
    p_atom_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM aios.reconcile_character_belief_atom_generic_v1(p_instance_id, p_atom_id);
    PERFORM aios.apply_character_belief_policy(p_instance_id, p_atom_id);
END;
$$;

COMMIT;
