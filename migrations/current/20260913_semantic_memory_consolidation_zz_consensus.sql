-- Follow-up policy for semantic memory consolidation.
-- Character memory remains authoritative for character-scoped evidence even
-- when a same-atom world state exists.  Cross-character agreement is exposed as
-- a promotion candidate, not silently converted into world truth.

BEGIN;

CREATE OR REPLACE FUNCTION aios.refresh_semantic_evidence_lifecycle(p_claim_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_proposition_id uuid;
    v_atom_id uuid;
    v_scope text;
    v_world_id uuid;
    v_character_id text;
    v_instance_id uuid;
    v_source_id text;
    v_context_confidence double precision;
    v_superseded_at timestamptz;
    v_stance text;
    v_surface_key text;
    v_status text := 'resolved';
    v_reason text := 'normalized_evidence';
BEGIN
    SELECT
        o.proposition_id,
        p.atom_id,
        ccr.epistemic_scope,
        ccr.world_id,
        ccr.origin_character_id,
        ccr.character_instance_id,
        ccr.source_id,
        ccr.confidence,
        ie.superseded_at
    INTO
        v_proposition_id,
        v_atom_id,
        v_scope,
        v_world_id,
        v_character_id,
        v_instance_id,
        v_source_id,
        v_context_confidence,
        v_superseded_at
    FROM aios.observation o
    JOIN aios.proposition p ON p.proposition_id=o.proposition_id
    LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
    LEFT JOIN aios.claim_candidate cc ON cc.claim_id=o.claim_id
    LEFT JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
    LEFT JOIN aios.document_section ds ON ds.section_id=es.section_id
    LEFT JOIN aios.dag_node dn ON dn.node_id=ds.node_id
    LEFT JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
    WHERE o.claim_id=p_claim_id
    ORDER BY o.observed_at DESC
    LIMIT 1;

    IF v_proposition_id IS NULL THEN
        INSERT INTO aios.semantic_evidence_lifecycle (
            claim_id, lifecycle_status, reason, updated_at
        )
        VALUES (p_claim_id, 'liminal', 'awaiting_normalization', now())
        ON CONFLICT (claim_id) DO UPDATE
        SET lifecycle_status='liminal',
            absorbed_into_surface_key=NULL,
            reason='awaiting_normalization',
            updated_at=now();
        RETURN;
    END IF;

    IF v_superseded_at IS NOT NULL THEN
        v_status := 'historical';
        v_reason := 'source_event_superseded';

    -- Character scope is epistemically prior to world representation.  A
    -- subjective belief must never become world truth just because its atom is
    -- also present in /world.
    ELSIF v_scope='character' AND v_instance_id IS NOT NULL AND EXISTS (
        SELECT 1
        FROM aios.character_belief_state bs
        WHERE bs.instance_id=v_instance_id
          AND bs.atom_id=v_atom_id
          AND bs.preferred_proposition_id IS NOT NULL
    ) THEN
        SELECT bs.stance,
               'char:' || COALESCE(v_character_id,v_instance_id::text)
               || ':instance:' || v_instance_id::text || ':atom:' || v_atom_id::text
        INTO v_stance, v_surface_key
        FROM aios.character_belief_state bs
        WHERE bs.instance_id=v_instance_id AND bs.atom_id=v_atom_id;
        v_status := CASE WHEN v_stance='unresolved' THEN 'contested' ELSE 'absorbed' END;
        v_reason := CASE WHEN v_stance='unresolved'
                         THEN 'represented_by_contested_character_memory'
                         ELSE 'represented_by_character_memory' END;

    ELSIF v_scope IS DISTINCT FROM 'character'
          AND v_world_id IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM aios.world_memory_state wms
              WHERE wms.world_id=v_world_id
                AND wms.atom_id=v_atom_id
                AND wms.preferred_proposition_id IS NOT NULL
          ) THEN
        SELECT wms.stance,
               'world:' || wms.world_id::text || ':atom:' || wms.atom_id::text
        INTO v_stance, v_surface_key
        FROM aios.world_memory_state wms
        WHERE wms.world_id=v_world_id AND wms.atom_id=v_atom_id;
        v_status := CASE WHEN v_stance='unresolved' THEN 'contested' ELSE 'promoted' END;
        v_reason := CASE WHEN v_stance='unresolved'
                         THEN 'represented_by_contested_world_memory'
                         ELSE 'represented_by_world_memory' END;

    ELSIF EXISTS (
        SELECT 1
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.semantic_evidence_admission sea
          ON sea.acquisition_id=kae.acquisition_id
        WHERE kae.claim_id=p_claim_id
        GROUP BY kae.claim_id
        HAVING bool_and(sea.status='suppressed')
    ) THEN
        v_status := 'rejected';
        v_reason := 'all_character_evidence_suppressed';
    ELSE
        v_status := 'resolved';
        v_reason := CASE
            WHEN v_source_id IS NOT NULL THEN 'resolved_source_evidence'
            WHEN v_world_id IS NOT NULL THEN 'resolved_world_evidence'
            ELSE 'resolved_unpromoted_evidence'
        END;
    END IF;

    INSERT INTO aios.semantic_evidence_lifecycle (
        claim_id, proposition_id, atom_id, scope_key, lifecycle_status,
        absorbed_into_surface_key, reason, confidence, meta,
        resolved_at, updated_at
    )
    VALUES (
        p_claim_id,
        v_proposition_id,
        v_atom_id,
        CASE
            WHEN v_scope='character' THEN 'char:' || COALESCE(v_character_id,v_instance_id::text)
            WHEN v_source_id IS NOT NULL THEN 'source:' || v_source_id
            WHEN v_world_id IS NOT NULL THEN 'world:' || v_world_id::text
            ELSE 'claim:' || p_claim_id::text
        END,
        v_status,
        v_surface_key,
        v_reason,
        v_context_confidence,
        jsonb_build_object(
            'epistemic_scope',v_scope,
            'world_id',v_world_id,
            'character_id',v_character_id,
            'character_instance_id',v_instance_id,
            'source_id',v_source_id,
            'evidence_preserved',true
        ),
        now(),
        now()
    )
    ON CONFLICT (claim_id) DO UPDATE
    SET proposition_id=EXCLUDED.proposition_id,
        atom_id=EXCLUDED.atom_id,
        scope_key=EXCLUDED.scope_key,
        lifecycle_status=EXCLUDED.lifecycle_status,
        absorbed_into_surface_key=EXCLUDED.absorbed_into_surface_key,
        reason=EXCLUDED.reason,
        confidence=EXCLUDED.confidence,
        meta=EXCLUDED.meta,
        resolved_at=COALESCE(aios.semantic_evidence_lifecycle.resolved_at,now()),
        updated_at=now();
END;
$$;

-- Candidate generation only.  This intentionally does not insert into
-- world_proposition_assertion.  The explicit world authority boundary remains
-- intact; a promotion validator must approve an eligible candidate.
CREATE OR REPLACE VIEW aios.world_consensus_candidate AS
WITH character_evidence AS (
    SELECT DISTINCT
        ccr.world_id,
        p.atom_id,
        ci.character_id,
        bs.instance_id,
        bs.stance,
        bs.belief_confidence,
        bs.preferred_proposition_id,
        COALESCE(ccr.claim_kind,'UNKNOWN') AS claim_kind
    FROM aios.character_belief_state bs
    JOIN aios.character_instance ci ON ci.instance_id=bs.instance_id
    JOIN aios.proposition p ON p.proposition_id=bs.preferred_proposition_id
    JOIN aios.observation o ON o.proposition_id=bs.preferred_proposition_id
    JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
    WHERE ccr.world_id IS NOT NULL
      AND bs.preferred_proposition_id IS NOT NULL
      AND COALESCE(ccr.claim_kind,'UNKNOWN') IN (
          'EVENT','STATE','LOCATION','OBJECT','ORGANIZATION','RULE'
      )
), aggregated AS (
    SELECT
        world_id,
        atom_id,
        COUNT(DISTINCT character_id) FILTER (
            WHERE stance='positive' AND belief_confidence >= 0.65
        ) AS supporting_characters,
        COUNT(DISTINCT character_id) FILTER (
            WHERE stance<>'positive' OR belief_confidence < 0.65
        ) AS disagreeing_characters,
        (ARRAY_AGG(
            preferred_proposition_id
            ORDER BY belief_confidence DESC, preferred_proposition_id
        ) FILTER (WHERE stance='positive' AND belief_confidence >= 0.65))[1]
            AS candidate_proposition_id,
        MIN(belief_confidence) FILTER (
            WHERE stance='positive' AND belief_confidence >= 0.65
        ) AS minimum_support_confidence,
        MAX(claim_kind) AS claim_kind
    FROM character_evidence
    GROUP BY world_id, atom_id
)
SELECT
    world_id,
    atom_id,
    candidate_proposition_id,
    claim_kind,
    supporting_characters,
    disagreeing_characters,
    minimum_support_confidence,
    CASE
        WHEN supporting_characters >= 2
         AND disagreeing_characters = 0
         AND candidate_proposition_id IS NOT NULL
            THEN 'eligible'
        WHEN disagreeing_characters > 0 THEN 'contested'
        ELSE 'insufficient_support'
    END AS candidate_status
FROM aggregated;

COMMENT ON VIEW aios.world_consensus_candidate IS
'Conservative cross-character agreement candidates for explicit /world promotion. Agreement never writes world truth by itself; promotion remains a separately validated authority action.';

-- Re-evaluate existing normalized evidence with the corrected character-first
-- precedence.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT claim_id
        FROM aios.semantic_evidence_lifecycle
        WHERE proposition_id IS NOT NULL
        ORDER BY updated_at, claim_id
    LOOP
        PERFORM aios.refresh_semantic_evidence_lifecycle(r.claim_id);
    END LOOP;
END;
$$;

COMMIT;
