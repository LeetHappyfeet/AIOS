-- AIOS semantic interpretation disagreement + belief RDF synchronization.
BEGIN;

-- ---------------------------------------------------------------------------
-- Semantic-frame policy v3.
--
-- The parser may choose a syntactically valid frame while local discourse still
-- supports more than one referent or copular interpretation.  Preserve those
-- candidates at the semantic-frame layer and quarantine close/unsafe choices
-- before proposition evidence can become active cognition.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION aios.semantic_frame_policy_v3()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_raw_text text;
    v_subject_candidates jsonb := '[]'::jsonb;
    v_object_candidates jsonb := '[]'::jsonb;
    v_subject_candidate_count integer := 0;
    v_object_candidate_count integer := 0;
    v_issue text;
    v_copular_class text;
BEGIN
    IF NEW.decomposer_version <> 'semantic-frame-v2' THEN
        RETURN NEW;
    END IF;

    SELECT cc.raw_text
    INTO v_raw_text
    FROM aios.claim_candidate cc
    WHERE cc.claim_id=NEW.claim_id;

    -- Third-person pronouns may legitimately have several recent PERSON
    -- antecedents.  Do not silently convert a close discourse disagreement into
    -- evidence.  First/second-person pivots are intentionally excluded because
    -- those are resolved by explicit speaker/viewpoint rules.
    IF lower(btrim(COALESCE(NEW.subject_text,''))) ~
       '^(he|him|his|she|her|hers|they|them|their|theirs)$' THEN
        WITH current_coord AS (
            SELECT es.section_id, es.sentence_index, dn.timeline_id, dn.created_at
            FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            JOIN aios.dag_node dn ON dn.node_id=ds.node_id
            WHERE cc.claim_id=NEW.claim_id
        ), recent_people AS (
            SELECT candidate, entity_key, recency_rank
            FROM (
                SELECT
                    COALESCE(f.resolved_subject, f.subject_text) AS candidate,
                    f.subject_entity_key AS entity_key,
                    row_number() OVER (ORDER BY dn.created_at DESC, es.sentence_index DESC, f.frame_index DESC) AS recency_rank
                FROM current_coord cur
                JOIN aios.dag_node dn ON dn.timeline_id=cur.timeline_id AND dn.created_at <= cur.created_at
                JOIN aios.document_section ds ON ds.node_id=dn.node_id
                JOIN aios.extracted_sentence es ON es.section_id=ds.section_id
                JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
                JOIN aios.claim_semantic_frame f ON f.claim_id=cc.claim_id
                WHERE f.frame_id <> NEW.frame_id
                  AND f.subject_kind_guess='PERSON'
                  AND COALESCE(f.resolved_subject, f.subject_text) IS NOT NULL
                UNION ALL
                SELECT
                    COALESCE(f.resolved_object, f.object_text) AS candidate,
                    f.object_entity_key AS entity_key,
                    row_number() OVER (ORDER BY dn.created_at DESC, es.sentence_index DESC, f.frame_index DESC) AS recency_rank
                FROM current_coord cur
                JOIN aios.dag_node dn ON dn.timeline_id=cur.timeline_id AND dn.created_at <= cur.created_at
                JOIN aios.document_section ds ON ds.node_id=dn.node_id
                JOIN aios.extracted_sentence es ON es.section_id=ds.section_id
                JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
                JOIN aios.claim_semantic_frame f ON f.claim_id=cc.claim_id
                WHERE f.frame_id <> NEW.frame_id
                  AND f.object_kind_guess='PERSON'
                  AND COALESCE(f.resolved_object, f.object_text) IS NOT NULL
            ) q
            WHERE candidate IS NOT NULL
            ORDER BY recency_rank
            LIMIT 8
        ), dedup AS (
            SELECT DISTINCT ON (lower(btrim(candidate))) candidate, entity_key, recency_rank
            FROM recent_people
            ORDER BY lower(btrim(candidate)), recency_rank
        )
        SELECT
            COALESCE(jsonb_agg(jsonb_build_object(
                'text', candidate,
                'entity_key', entity_key,
                'recency_rank', recency_rank
            ) ORDER BY recency_rank), '[]'::jsonb),
            COUNT(*)
        INTO v_subject_candidates, v_subject_candidate_count
        FROM dedup;
    END IF;

    IF lower(btrim(COALESCE(NEW.object_text,''))) ~
       '^(he|him|his|she|her|hers|they|them|their|theirs)([[:space:]]|$)' THEN
        WITH current_coord AS (
            SELECT es.section_id, es.sentence_index, dn.timeline_id, dn.created_at
            FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            JOIN aios.dag_node dn ON dn.node_id=ds.node_id
            WHERE cc.claim_id=NEW.claim_id
        ), recent_people AS (
            SELECT candidate, entity_key, recency_rank
            FROM (
                SELECT
                    COALESCE(f.resolved_subject, f.subject_text) AS candidate,
                    f.subject_entity_key AS entity_key,
                    row_number() OVER (ORDER BY dn.created_at DESC, es.sentence_index DESC, f.frame_index DESC) AS recency_rank
                FROM current_coord cur
                JOIN aios.dag_node dn ON dn.timeline_id=cur.timeline_id AND dn.created_at <= cur.created_at
                JOIN aios.document_section ds ON ds.node_id=dn.node_id
                JOIN aios.extracted_sentence es ON es.section_id=ds.section_id
                JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
                JOIN aios.claim_semantic_frame f ON f.claim_id=cc.claim_id
                WHERE f.frame_id <> NEW.frame_id
                  AND f.subject_kind_guess='PERSON'
                  AND COALESCE(f.resolved_subject, f.subject_text) IS NOT NULL
                UNION ALL
                SELECT
                    COALESCE(f.resolved_object, f.object_text) AS candidate,
                    f.object_entity_key AS entity_key,
                    row_number() OVER (ORDER BY dn.created_at DESC, es.sentence_index DESC, f.frame_index DESC) AS recency_rank
                FROM current_coord cur
                JOIN aios.dag_node dn ON dn.timeline_id=cur.timeline_id AND dn.created_at <= cur.created_at
                JOIN aios.document_section ds ON ds.node_id=dn.node_id
                JOIN aios.extracted_sentence es ON es.section_id=ds.section_id
                JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
                JOIN aios.claim_semantic_frame f ON f.claim_id=cc.claim_id
                WHERE f.frame_id <> NEW.frame_id
                  AND f.object_kind_guess='PERSON'
                  AND COALESCE(f.resolved_object, f.object_text) IS NOT NULL
            ) q
            WHERE candidate IS NOT NULL
            ORDER BY recency_rank
            LIMIT 8
        ), dedup AS (
            SELECT DISTINCT ON (lower(btrim(candidate))) candidate, entity_key, recency_rank
            FROM recent_people
            ORDER BY lower(btrim(candidate)), recency_rank
        )
        SELECT
            COALESCE(jsonb_agg(jsonb_build_object(
                'text', candidate,
                'entity_key', entity_key,
                'recency_rank', recency_rank
            ) ORDER BY recency_rank), '[]'::jsonb),
            COUNT(*)
        INTO v_object_candidates, v_object_candidate_count
        FROM dedup;
    END IF;

    IF v_subject_candidate_count > 1 THEN
        v_issue := 'subject_referent_disagreement';
    ELSIF v_object_candidate_count > 1 THEN
        v_issue := 'object_referent_disagreement';
    END IF;

    -- Reclassify copular frames conservatively instead of treating every form
    -- of "be" as an identity/definition assertion.
    IF NEW.predicate_canonical='be_definition_of' THEN
        IF NULLIF(btrim(COALESCE(NEW.resolved_subject, NEW.subject_text, '')), '') IS NOT NULL
           AND lower(btrim(COALESCE(NEW.resolved_subject, NEW.subject_text, '')))
             = lower(btrim(COALESCE(NEW.resolved_object, NEW.object_text, ''))) THEN
            v_issue := COALESCE(v_issue, 'semantic_self_definition');
            v_copular_class := 'self_definition';
        ELSIF NEW.object_kind_guess='LOCATION'
           OR lower(COALESCE(v_raw_text,'')) ~
              '\m(is|are|was|were|be|been|being)\M[[:space:]]+(in|at|on|inside|outside|near|within|beside|under|over)\M' THEN
            NEW.predicate_canonical := 'located_at';
            v_copular_class := 'spatial';
        ELSIF NEW.object_kind_guess IN ('PERSON','ORGANIZATION')
           OR lower(btrim(COALESCE(NEW.object_text,''))) ~ '^(a|an)[[:space:]]+[[:alnum:]_-]+([[:space:]][[:alnum:]_-]+){0,3}$' THEN
            NEW.predicate_canonical := 'identity';
            v_copular_class := 'identity';
        ELSIF NEW.object_kind_guess IN ('TIME','EVENT','ACTION','QUANTITY','MEMORY','RELATIONSHIP','GOAL','RULE') THEN
            v_issue := COALESCE(v_issue, 'copular_role_disagreement');
            v_copular_class := 'incompatible';
        ELSIF array_length(regexp_split_to_array(btrim(COALESCE(NEW.object_text,'')), '\s+'), 1) <= 3
              AND lower(btrim(COALESCE(NEW.object_text,''))) !~ '^(the|a|an)[[:space:]]' THEN
            NEW.predicate_canonical := 'state';
            v_copular_class := 'descriptive_state';
        ELSE
            v_issue := COALESCE(v_issue, 'copular_semantic_class_unresolved');
            v_copular_class := 'unresolved';
        END IF;
    END IF;

    IF v_issue IS NOT NULL THEN
        NEW.resolution_status := 'ambiguous';
        NEW.frame_confidence := LEAST(NEW.frame_confidence, 0.54);
    END IF;

    NEW.resolver_version := 'semantic-frame-policy-v3';
    NEW.meta := COALESCE(NEW.meta, '{}'::jsonb) || jsonb_build_object(
        'interpretation_policy', 'semantic-frame-policy-v3',
        'interpretation_status', CASE WHEN v_issue IS NULL THEN 'accepted' ELSE 'ambiguous' END,
        'interpretation_issue', v_issue,
        'subject_candidates', v_subject_candidates,
        'object_candidates', v_object_candidates,
        'copular_class', v_copular_class
    );

    IF NEW.canonical_text IS NOT NULL AND NEW.predicate_canonical IS NOT NULL THEN
        NEW.canonical_text :=
            CASE WHEN NEW.polarity < 0 THEN 'NOT ' ELSE '' END
            || COALESCE(NEW.resolved_subject, NEW.subject_text, '_')
            || ' | ' || NEW.predicate_canonical
            || ' | ' || COALESCE(NEW.resolved_object, NEW.object_text, '_');
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_frame_policy_v3 ON aios.claim_semantic_frame;
CREATE TRIGGER trg_semantic_frame_policy_v3
BEFORE UPDATE OF resolved_subject, resolved_object, resolution_status,
                 referent_confidence, frame_confidence, predicate_canonical
ON aios.claim_semantic_frame
FOR EACH ROW
EXECUTE FUNCTION aios.semantic_frame_policy_v3();

-- Apply the policy to existing resolved frames without deleting evidence.
UPDATE aios.claim_semantic_frame
SET resolution_status=resolution_status
WHERE decomposer_version='semantic-frame-v2'
  AND resolution_status IN ('resolved','partial','ambiguous');

-- ---------------------------------------------------------------------------
-- Admission must follow the exact semantic frame that produced a proposition,
-- not only the claim's primary frame.  Ambiguous interpretations remain
-- durable evidence but cannot become active /char support.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION aios.enforce_frame_interpretation_admission()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_frame_status text;
    v_frame_confidence double precision;
    v_frame_meta jsonb;
BEGIN
    SELECT sf.resolution_status, sf.frame_confidence, sf.meta
    INTO v_frame_status, v_frame_confidence, v_frame_meta
    FROM aios.knowledge_acquisition_event kae
    JOIN aios.observation o ON o.claim_id=kae.claim_id
    JOIN aios.observation_proposition op
      ON op.observation_id=o.observation_id
     AND op.proposition_id=kae.proposition_id
    JOIN aios.claim_semantic_frame sf ON sf.frame_id=op.frame_id
    WHERE kae.acquisition_id=NEW.acquisition_id
    ORDER BY op.is_primary DESC, sf.frame_confidence DESC, sf.frame_index
    LIMIT 1;

    IF NOT FOUND THEN
        SELECT sf.resolution_status, sf.frame_confidence, sf.meta
        INTO v_frame_status, v_frame_confidence, v_frame_meta
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.claim_semantic_frame_projection sfp ON sfp.claim_id=kae.claim_id
        JOIN aios.claim_semantic_frame sf ON sf.frame_id=sfp.primary_frame_id
        WHERE kae.acquisition_id=NEW.acquisition_id;
    END IF;

    IF v_frame_status='ambiguous'
       OR COALESCE(v_frame_meta->>'interpretation_status','')='ambiguous' THEN
        NEW.status := 'unresolved';
        NEW.reason := 'semantic_interpretation_disagreement';
        NEW.confidence := LEAST(NEW.confidence, COALESCE(v_frame_confidence, 0.54));
        NEW.meta := COALESCE(NEW.meta, '{}'::jsonb) || jsonb_build_object(
            'frame_interpretation_status', v_frame_status,
            'frame_interpretation_issue', v_frame_meta->>'interpretation_issue',
            'frame_interpretation_policy', v_frame_meta->>'interpretation_policy'
        );
    ELSIF v_frame_status='partial' THEN
        NEW.status := 'unresolved';
        NEW.reason := 'semantic_frame_partial';
        NEW.confidence := LEAST(NEW.confidence, COALESCE(v_frame_confidence, 0.54));
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_enforce_frame_interpretation_admission
ON aios.semantic_evidence_admission;
CREATE TRIGGER trg_enforce_frame_interpretation_admission
BEFORE INSERT OR UPDATE
ON aios.semantic_evidence_admission
FOR EACH ROW
EXECUTE FUNCTION aios.enforce_frame_interpretation_admission();

-- Re-apply only to acquisitions that can be tied to a semantic frame.  Changes
-- to status/confidence automatically refresh character_belief_state through the
-- existing belief-reconciliation triggers.
UPDATE aios.semantic_evidence_admission sea
SET status=sea.status,
    confidence=sea.confidence,
    updated_at=now()
FROM aios.knowledge_acquisition_event kae
JOIN aios.observation o ON o.claim_id=kae.claim_id
JOIN aios.observation_proposition op
  ON op.observation_id=o.observation_id
 AND op.proposition_id=kae.proposition_id
JOIN aios.claim_semantic_frame sf ON sf.frame_id=op.frame_id
WHERE sea.acquisition_id=kae.acquisition_id
  AND sf.resolution_status IN ('ambiguous','partial');

-- ---------------------------------------------------------------------------
-- Belief RDF dirty boundary.
--
-- RDF projection is performed by the existing rdf_epistemic_project worker.
-- When a reconciled belief changes, invalidate one stable claim receipt for the
-- affected instance.  The existing supervisor will enqueue one RDF job, which
-- can synchronize the whole instance belief state without creating a second
-- scheduler or mixing RDF I/O into SQL triggers.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION aios.mark_character_belief_rdf_dirty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_instance_id uuid;
    v_seed_claim_id uuid;
BEGIN
    v_instance_id := CASE WHEN TG_OP='DELETE' THEN OLD.instance_id ELSE NEW.instance_id END;

    SELECT kae.claim_id
    INTO v_seed_claim_id
    FROM aios.knowledge_acquisition_event kae
    WHERE kae.instance_id=v_instance_id
      AND kae.claim_id IS NOT NULL
    ORDER BY kae.created_at DESC, kae.acquisition_id DESC
    LIMIT 1;

    IF v_seed_claim_id IS NOT NULL THEN
        DELETE FROM aios.rdf_promotion_log
        WHERE claim_id=v_seed_claim_id
          AND rdf_dataset='world'
          AND rdf_graph='urn:aios:world:epistemic'
          AND rdf_predicate='world:observesProposition';
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mark_character_belief_rdf_dirty
ON aios.character_belief_state;
CREATE TRIGGER trg_mark_character_belief_rdf_dirty
AFTER INSERT OR UPDATE OF stance, positive_support, negative_support,
                      belief_confidence, preferred_proposition_id OR DELETE
ON aios.character_belief_state
FOR EACH ROW
EXECUTE FUNCTION aios.mark_character_belief_rdf_dirty();

-- Backfill one dirty receipt per existing instance so current positive,
-- negative, and unresolved beliefs are projected after this migration.
WITH seed AS (
    SELECT DISTINCT ON (kae.instance_id)
        kae.instance_id,
        kae.claim_id
    FROM aios.knowledge_acquisition_event kae
    WHERE kae.claim_id IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM aios.character_belief_state bs
          WHERE bs.instance_id=kae.instance_id
      )
    ORDER BY kae.instance_id, kae.created_at DESC, kae.acquisition_id DESC
)
DELETE FROM aios.rdf_promotion_log rpl
USING seed s
WHERE rpl.claim_id=s.claim_id
  AND rpl.rdf_dataset='world'
  AND rpl.rdf_graph='urn:aios:world:epistemic'
  AND rpl.rdf_predicate='world:observesProposition';

COMMIT;
