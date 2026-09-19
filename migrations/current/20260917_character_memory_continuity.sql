-- Character memory continuity policy and cross-session cognitive scope.
BEGIN;

ALTER TABLE aios.character_epistemic_profile
    ADD COLUMN IF NOT EXISTS memory_continuity text NOT NULL DEFAULT 'character';

ALTER TABLE aios.character_epistemic_profile
    DROP CONSTRAINT IF EXISTS character_epistemic_profile_memory_continuity_check;
ALTER TABLE aios.character_epistemic_profile
    ADD CONSTRAINT character_epistemic_profile_memory_continuity_check
    CHECK (memory_continuity IN ('isolated','user','character'));

COMMENT ON COLUMN aios.character_epistemic_profile.memory_continuity IS
'Controls durable cognitive evidence scope for a character_id. isolated=experiential lineage only; user=same character/user plus lineage; character=all instances of the character. Defaults to character.';

CREATE OR REPLACE FUNCTION aios.cognitive_evidence_instances(p_instance_id uuid)
RETURNS TABLE(instance_id uuid, depth integer)
LANGUAGE sql
STABLE
AS $$
WITH RECURSIVE
target AS (
    SELECT
        ci.instance_id,
        ci.character_id,
        ci.meta->>'runtime_user_name' AS runtime_user_name,
        COALESCE(cep.memory_continuity, 'character') AS memory_continuity
    FROM aios.character_instance ci
    LEFT JOIN aios.character_epistemic_profile cep
      ON cep.character_id=ci.character_id
    WHERE ci.instance_id=p_instance_id
),
lineage AS (
    SELECT ci.instance_id, ci.parent_instance_id, 0 AS depth
    FROM aios.character_instance ci
    WHERE ci.instance_id=p_instance_id

    UNION ALL

    SELECT parent.instance_id, parent.parent_instance_id, lineage.depth + 1
    FROM lineage
    JOIN aios.character_instance parent
      ON parent.instance_id=lineage.parent_instance_id
    WHERE lineage.depth < 64
),
eligible AS (
    SELECT l.instance_id, l.depth
    FROM lineage l

    UNION ALL

    SELECT
        ci.instance_id,
        100000 + row_number() OVER (
            ORDER BY ci.created_at DESC, ci.instance_id
        )::integer AS depth
    FROM target t
    JOIN aios.character_instance ci
      ON ci.character_id=t.character_id
    WHERE t.memory_continuity='character'
       OR (
            t.memory_continuity='user'
            AND t.runtime_user_name IS NOT NULL
            AND ci.meta->>'runtime_user_name'=t.runtime_user_name
       )
)
SELECT instance_id, MIN(depth)::integer AS depth
FROM eligible
GROUP BY instance_id
ORDER BY MIN(depth), instance_id;
$$;

COMMENT ON FUNCTION aios.cognitive_evidence_instances(uuid) IS
'Canonical durable-memory scope. Source timelines remain generation coordinates and do not constrain historical cognition.';

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
        SELECT candidate.instance_id
        FROM aios.character_instance source
        JOIN aios.character_instance candidate
          ON candidate.character_id=source.character_id
        WHERE source.instance_id=p_source_instance_id
          AND EXISTS (
              SELECT 1
              FROM aios.cognitive_evidence_instances(candidate.instance_id) eligible
              WHERE eligible.instance_id=p_source_instance_id
          )
        ORDER BY candidate.created_at, candidate.instance_id
    LOOP
        PERFORM aios.reconcile_character_belief_atom(rec.instance_id, p_atom_id);
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION aios.refresh_beliefs_from_memory_continuity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
BEGIN
    IF TG_OP='UPDATE' AND OLD.memory_continuity IS NOT DISTINCT FROM NEW.memory_continuity THEN
        RETURN NEW;
    END IF;

    FOR rec IN
        SELECT target.instance_id, atoms.atom_id
        FROM aios.character_instance target
        CROSS JOIN LATERAL (
            SELECT DISTINCT p.atom_id
            FROM aios.character_proposition_knowledge cpk
            JOIN aios.character_instance evidence_ci
              ON evidence_ci.instance_id=cpk.instance_id
            JOIN aios.proposition p ON p.proposition_id=cpk.proposition_id
            WHERE evidence_ci.character_id=NEW.character_id
              AND p.atom_id IS NOT NULL

            UNION

            SELECT bs.atom_id
            FROM aios.character_belief_state bs
            WHERE bs.instance_id=target.instance_id
        ) atoms
        WHERE target.character_id=NEW.character_id
        ORDER BY target.instance_id, atoms.atom_id
    LOOP
        PERFORM aios.reconcile_character_belief_atom(rec.instance_id, rec.atom_id);
    END LOOP;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_beliefs_from_memory_continuity
ON aios.character_epistemic_profile;
CREATE TRIGGER trg_refresh_beliefs_from_memory_continuity
AFTER INSERT OR UPDATE OF memory_continuity
ON aios.character_epistemic_profile
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_beliefs_from_memory_continuity();

-- Reconcile existing targets so the new default is visible immediately.
DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT DISTINCT ci.instance_id, p.atom_id
        FROM aios.character_instance ci
        CROSS JOIN LATERAL aios.cognitive_evidence_instances(ci.instance_id) eligible
        JOIN aios.character_proposition_knowledge cpk
          ON cpk.instance_id=eligible.instance_id
        JOIN aios.proposition p
          ON p.proposition_id=cpk.proposition_id
        WHERE p.atom_id IS NOT NULL
        ORDER BY ci.instance_id, p.atom_id
    LOOP
        PERFORM aios.reconcile_character_belief_atom(rec.instance_id, rec.atom_id);
    END LOOP;
END;
$$;

COMMIT;
