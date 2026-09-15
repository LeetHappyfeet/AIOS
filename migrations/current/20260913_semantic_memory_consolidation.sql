-- AIOS semantic memory consolidation boundary.
--
-- Raw claims remain immutable evidence.  Reconciled character/world state is
-- the durable memory surface used by retrieval.  Liminal RDF becomes staging:
-- once evidence is represented by durable memory (or has become historical),
-- its liminal projection may be compacted without deleting PostgreSQL provenance.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_evidence_lifecycle (
    claim_id uuid PRIMARY KEY
        REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    proposition_id uuid
        REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    atom_id uuid
        REFERENCES aios.semantic_atom(atom_id) ON DELETE SET NULL,
    scope_key text,
    lifecycle_status text NOT NULL DEFAULT 'liminal'
        CHECK (lifecycle_status IN (
            'liminal','resolved','absorbed','contested','promoted','rejected','historical'
        )),
    absorbed_into_surface_key text,
    reason text NOT NULL DEFAULT 'awaiting_semantic_resolution',
    confidence double precision
        CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_semantic_evidence_lifecycle_status
    ON aios.semantic_evidence_lifecycle (lifecycle_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_semantic_evidence_lifecycle_atom
    ON aios.semantic_evidence_lifecycle (atom_id, lifecycle_status)
    WHERE atom_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_semantic_evidence_lifecycle_surface
    ON aios.semantic_evidence_lifecycle (absorbed_into_surface_key)
    WHERE absorbed_into_surface_key IS NOT NULL;

COMMENT ON TABLE aios.semantic_evidence_lifecycle IS
'Lifecycle of claim evidence after atomization. Claims are never merged away; absorbed/promoted claims are represented by durable semantic memory and may leave active liminal RDF.';

-- Canonical read surface.  Retrieval code should prefer this layer over raw
-- observations/acquisitions.  It intentionally exposes one row per reconciled
-- semantic atom and preserves unresolved polarity as state rather than forcing a
-- winner in the HUD.
CREATE OR REPLACE VIEW aios.semantic_memory_surface AS
SELECT
    'char:' || ci.character_id || ':instance:' || bs.instance_id::text || ':atom:' || bs.atom_id::text AS surface_key,
    'character'::text AS scope_kind,
    'char:' || ci.character_id AS scope_key,
    ci.character_id,
    bs.instance_id,
    NULL::uuid AS world_id,
    bs.atom_id,
    bs.preferred_proposition_id,
    COALESCE(ctx.claim_kind, 'BELIEF')::text AS memory_kind,
    bs.stance,
    bs.belief_confidence AS confidence,
    bs.positive_support,
    bs.negative_support,
    bs.evidence_count,
    bs.independent_evidence_count,
    p.canonical_text,
    p.subject_norm,
    p.predicate_norm,
    p.object_norm,
    p.polarity,
    bs.updated_at,
    bs.meta || jsonb_build_object(
        'surface_source','character_belief_state',
        'preferred_evidence_instance_id',bs.preferred_evidence_instance_id
    ) AS meta
FROM aios.character_belief_state bs
JOIN aios.character_instance ci ON ci.instance_id=bs.instance_id
LEFT JOIN aios.proposition p ON p.proposition_id=bs.preferred_proposition_id
LEFT JOIN LATERAL (
    SELECT ccr.claim_kind
    FROM aios.observation o
    JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
    WHERE o.proposition_id=bs.preferred_proposition_id
    ORDER BY ccr.resolved_at DESC NULLS LAST, o.observed_at DESC
    LIMIT 1
) ctx ON true
WHERE bs.preferred_proposition_id IS NOT NULL

UNION ALL

SELECT
    'world:' || wms.world_id::text || ':atom:' || wms.atom_id::text AS surface_key,
    'world'::text AS scope_kind,
    'world:' || wms.world_id::text AS scope_key,
    NULL::text AS character_id,
    NULL::uuid AS instance_id,
    wms.world_id,
    wms.atom_id,
    wms.preferred_proposition_id,
    'WORLD_FACT'::text AS memory_kind,
    wms.stance,
    wms.state_confidence AS confidence,
    wms.positive_support,
    wms.negative_support,
    wms.evidence_count,
    wms.independent_evidence_count,
    p.canonical_text,
    p.subject_norm,
    p.predicate_norm,
    p.object_norm,
    p.polarity,
    wms.updated_at,
    wms.meta || jsonb_build_object(
        'surface_source','world_memory_state',
        'preferred_assertion_id',wms.preferred_assertion_id
    ) AS meta
FROM aios.world_memory_state wms
LEFT JOIN aios.proposition p ON p.proposition_id=wms.preferred_proposition_id
WHERE wms.preferred_proposition_id IS NOT NULL;

COMMENT ON VIEW aios.semantic_memory_surface IS
'Canonical compact semantic memory surface. HUD/retrieval consumes reconciled state here; raw claims and acquisitions remain provenance underneath.';

-- Refresh one claim lifecycle from authoritative current semantic state.
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
        SET lifecycle_status='liminal', reason='awaiting_normalization', updated_at=now();
        RETURN;
    END IF;

    IF v_superseded_at IS NOT NULL THEN
        v_status := 'historical';
        v_reason := 'source_event_superseded';
    ELSIF v_world_id IS NOT NULL AND EXISTS (
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

CREATE OR REPLACE FUNCTION aios.refresh_semantic_lifecycle_from_observation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM aios.refresh_semantic_evidence_lifecycle(NEW.claim_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_semantic_lifecycle_observation ON aios.observation;
CREATE TRIGGER trg_refresh_semantic_lifecycle_observation
AFTER INSERT OR UPDATE OF proposition_id
ON aios.observation
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_semantic_lifecycle_from_observation();

CREATE OR REPLACE FUNCTION aios.refresh_semantic_lifecycle_from_belief_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT DISTINCT kae.claim_id
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
        WHERE kae.instance_id=NEW.instance_id
          AND p.atom_id=NEW.atom_id
          AND kae.claim_id IS NOT NULL
    LOOP
        PERFORM aios.refresh_semantic_evidence_lifecycle(r.claim_id);
    END LOOP;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_semantic_lifecycle_belief_state ON aios.character_belief_state;
CREATE TRIGGER trg_refresh_semantic_lifecycle_belief_state
AFTER INSERT OR UPDATE
ON aios.character_belief_state
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_semantic_lifecycle_from_belief_state();

CREATE OR REPLACE FUNCTION aios.refresh_semantic_lifecycle_from_world_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT DISTINCT o.claim_id
        FROM aios.observation o
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
        WHERE ccr.world_id=NEW.world_id
          AND p.atom_id=NEW.atom_id
    LOOP
        PERFORM aios.refresh_semantic_evidence_lifecycle(r.claim_id);
    END LOOP;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_semantic_lifecycle_world_state ON aios.world_memory_state;
CREATE TRIGGER trg_refresh_semantic_lifecycle_world_state
AFTER INSERT OR UPDATE
ON aios.world_memory_state
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_semantic_lifecycle_from_world_state();

-- Liminal is an active staging projection, not durable truth.  Terminal
-- lifecycle states are safe to remove from Fuseki because claim/proposition/
-- provenance rows remain in PostgreSQL and the durable memory surface carries
-- current meaning.
CREATE OR REPLACE VIEW aios.liminal_compaction_candidate AS
SELECT
    sel.claim_id,
    sel.proposition_id,
    sel.atom_id,
    sel.lifecycle_status,
    sel.absorbed_into_surface_key,
    sel.reason,
    sel.updated_at
FROM aios.semantic_evidence_lifecycle sel
WHERE sel.lifecycle_status IN ('absorbed','promoted','rejected','historical')
  AND EXISTS (
      SELECT 1
      FROM aios.rdf_promotion_log rpl
      WHERE rpl.claim_id=sel.claim_id
        AND rpl.rdf_dataset='world'
        AND rpl.rdf_graph='urn:aios:world:liminal'
        AND rpl.rdf_predicate='rdf:type'
        AND rpl.rdf_object='world:Claim'
        AND COALESCE(rpl.promotion_meta,'{}'::jsonb) ? 'compacted_at' = false
  );

COMMENT ON VIEW aios.liminal_compaction_candidate IS
'Claims whose active liminal RDF can be removed because durable semantic state or historical provenance now represents them.';

-- Seed lifecycle for existing evidence without rewriting or deleting history.
INSERT INTO aios.semantic_evidence_lifecycle (
    claim_id, proposition_id, atom_id, scope_key, lifecycle_status,
    reason, confidence, resolved_at, updated_at
)
SELECT
    cc.claim_id,
    o.proposition_id,
    p.atom_id,
    COALESCE(mrr.scope_key, 'claim:' || cc.claim_id::text),
    CASE WHEN o.proposition_id IS NULL THEN 'liminal' ELSE 'resolved' END,
    CASE WHEN o.proposition_id IS NULL THEN 'awaiting_normalization' ELSE 'migration_backfill' END,
    ccr.confidence,
    CASE WHEN o.proposition_id IS NULL THEN NULL ELSE now() END,
    now()
FROM aios.claim_candidate cc
LEFT JOIN aios.observation o ON o.claim_id=cc.claim_id
LEFT JOIN aios.proposition p ON p.proposition_id=o.proposition_id
LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
LEFT JOIN aios.memory_reconciliation_receipt mrr ON mrr.claim_id=cc.claim_id
ON CONFLICT (claim_id) DO NOTHING;

-- Re-evaluate already normalized claims against existing reconciled memory.
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
