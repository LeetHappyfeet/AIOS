-- AIOS character belief reconciliation phase 2.
BEGIN;

-- ---------------------------------------------------------------------------
-- Current character belief state.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aios.belief_reconciliation_policy (
    policy_key text PRIMARY KEY,
    accept_support double precision NOT NULL CHECK (accept_support BETWEEN 0 AND 1),
    decision_margin double precision NOT NULL CHECK (decision_margin BETWEEN 0 AND 1),
    resolver_version text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO aios.belief_reconciliation_policy (
    policy_key, accept_support, decision_margin, resolver_version
)
VALUES ('default', 0.60, 0.15, 'character-belief-v1')
ON CONFLICT (policy_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS aios.character_belief_state (
    instance_id uuid NOT NULL
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    atom_id uuid NOT NULL
        REFERENCES aios.semantic_atom(atom_id) ON DELETE CASCADE,
    stance text NOT NULL CHECK (stance IN ('positive','negative','unresolved')),
    positive_support double precision NOT NULL DEFAULT 0.0 CHECK (positive_support BETWEEN 0 AND 1),
    negative_support double precision NOT NULL DEFAULT 0.0 CHECK (negative_support BETWEEN 0 AND 1),
    belief_confidence double precision NOT NULL DEFAULT 0.0 CHECK (belief_confidence BETWEEN 0 AND 1),
    preferred_proposition_id uuid
        REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    preferred_evidence_instance_id uuid
        REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL,
    evidence_count integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    independent_evidence_count integer NOT NULL DEFAULT 0 CHECK (independent_evidence_count >= 0),
    resolved_through_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    resolver_version text NOT NULL DEFAULT 'character-belief-v1',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, atom_id)
);

CREATE INDEX IF NOT EXISTS idx_character_belief_state_instance
    ON aios.character_belief_state (instance_id, stance, belief_confidence DESC);

COMMENT ON TABLE aios.character_belief_state IS
'Convergent current /char belief state. Evidence remains in character_proposition_knowledge and acquisition topology.';

CREATE OR REPLACE FUNCTION aios.reconcile_character_belief_atom(
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
    INTO v_accept_support, v_decision_margin
    FROM aios.belief_reconciliation_policy
    WHERE policy_key='default';

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
            COALESCE(
                kae.meta->>'source_key',
                cpk.meta->>'source_key',
                kae.source_entity_id::text,
                'unknown'
            ) AS source_key,
            COALESCE(
                kae.dag_node_id::text,
                cpk.last_node_id::text,
                kae.acquisition_id::text
            ) AS event_key,
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
            source_key || ':' || event_key AS correlation_key,
            MAX(weight) AS weight
        FROM raw_evidence
        GROUP BY polarity, source_key || ':' || event_key
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
        'character-belief-v1',
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
        1.0, jsonb_build_object('belief_resolver_version', 'character-belief-v1')
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
        1.0, jsonb_build_object('belief_resolver_version', 'character-belief-v1')
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
        jsonb_build_object('belief_resolver_version', 'character-belief-v1')
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
            'resolver_version', 'character-belief-v1'
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
            'resolver_version', 'character-belief-v1'
        )
    )
    ON CONFLICT (scope_key, parent_node_id, child_node_id, edge_type) DO UPDATE
    SET significance=EXCLUDED.significance,
        meta=EXCLUDED.meta;

    DELETE FROM aios.semantic_topology_edge
    WHERE scope_key=v_scope_key
      AND parent_node_id=v_belief_node_id
      AND edge_type IN ('supports_belief_atom','opposes_belief_atom');

    WITH RECURSIVE lineage AS (
        SELECT ci.instance_id, ci.parent_instance_id
        FROM aios.character_instance ci
        WHERE ci.instance_id=p_instance_id

        UNION ALL

        SELECT parent.instance_id, parent.parent_instance_id
        FROM lineage
        JOIN aios.character_instance parent
          ON parent.instance_id=lineage.parent_instance_id
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
            'resolver_version', 'character-belief-v1'
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

CREATE OR REPLACE FUNCTION aios.reconcile_belief_descendants(
    p_source_instance_id uuid,
    p_atom_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        WITH RECURSIVE descendants AS (
            SELECT ci.instance_id
            FROM aios.character_instance ci
            WHERE ci.instance_id=p_source_instance_id

            UNION ALL

            SELECT child.instance_id
            FROM aios.character_instance child
            JOIN descendants parent
              ON child.parent_instance_id=parent.instance_id
        )
        SELECT instance_id
        FROM descendants
    LOOP
        PERFORM aios.reconcile_character_belief_atom(rec.instance_id, p_atom_id);
    END LOOP;
END;
$$;

COMMIT;