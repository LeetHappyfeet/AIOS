-- Separate evidence origin from character perception/acquisition.
--
-- knowledge_acquisition_event.instance_id is the perceiver/learner.  It is not
-- the origin character instance of the source claim.  Source/origin identity
-- remains on claim_context_resolution / observation and is copied into
-- acquisition metadata for provenance and diagnostics.
--
-- This migration deliberately leaves observations, propositions, existing
-- acquisition rows, evidence topology, and reconciled belief state intact.
-- Existing live-chat acquisitions are repaired in place and re-queued for the
-- normal character-knowledge projector; missing listener acquisitions are
-- backfilled as new append-only acquisition events.

BEGIN;

CREATE OR REPLACE FUNCTION aios.acquisition_mode_for_perceiver(
    p_node_kind text,
    p_origin_instance_id uuid,
    p_perceiver_instance_id uuid
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_node_kind='chat_message'
         AND p_origin_instance_id IS NOT NULL
         AND p_origin_instance_id=p_perceiver_instance_id
            THEN 'self_utterance'
        WHEN p_node_kind='chat_message'
            THEN 'conversation'
        WHEN p_node_kind='observation'
            THEN 'direct_perception'
        ELSE 'observed_source'
    END
$$;

COMMENT ON FUNCTION aios.acquisition_mode_for_perceiver(text, uuid, uuid) IS
'Returns acquisition mode from the perceiver perspective. Source transport/origin mode remains provenance metadata.';

CREATE OR REPLACE FUNCTION aios.project_observation_to_runtime_perceivers()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
    v_mode text;
    v_epistemic_status text;
BEGIN
    -- Only event forms that an attached live runtime can directly perceive are
    -- projected automatically. Documents/source material still require an
    -- explicit read/learn acquisition path.
    FOR rec IN
        SELECT
            ci.instance_id AS perceiver_instance_id,
            ci.character_id AS perceiver_character_id,
            ccr.origin_character_id,
            ccr.character_instance_id AS origin_instance_id,
            ccr.acquisition_mode AS source_acquisition_mode,
            ccr.claim_kind,
            dn.kind::text AS node_kind,
            dn.speaker_id,
            dn.speaker_role::text AS speaker_role,
            rt.timeline_id AS runtime_timeline_id,
            st.timeline_id AS source_timeline_id
        FROM aios.character_runtime_state rs
        JOIN aios.character_instance ci
          ON ci.instance_id=rs.instance_id
        JOIN aios.timeline rt
          ON rt.timeline_id=rs.timeline_id
        JOIN aios.timeline st
          ON st.timeline_id=NEW.timeline_id
        JOIN aios.dag_node dn
          ON dn.node_id=NEW.dag_node_id
        LEFT JOIN aios.claim_context_resolution ccr
          ON ccr.claim_id=NEW.claim_id
        WHERE rs.source_timeline_id=NEW.timeline_id
          AND rt.session_id IS NOT DISTINCT FROM st.session_id
          AND rt.user_name IS NOT DISTINCT FROM st.user_name
          AND rt.scope_key=st.scope_key
          AND dn.kind::text IN ('chat_message','observation')
    LOOP
        v_mode := aios.acquisition_mode_for_perceiver(
            rec.node_kind,
            rec.origin_instance_id,
            rec.perceiver_instance_id
        );

        v_epistemic_status := CASE upper(COALESCE(rec.claim_kind, 'UNKNOWN'))
            WHEN 'BELIEF' THEN 'believed'
            WHEN 'MEMORY' THEN 'remembered'
            WHEN 'GOAL' THEN 'intended'
            WHEN 'RULE' THEN 'accepted_rule'
            ELSE 'observed'
        END;

        INSERT INTO aios.knowledge_acquisition_event (
            instance_id,
            proposition_id,
            claim_id,
            acquisition_mode,
            epistemic_status,
            confidence,
            dag_node_id,
            meta
        )
        SELECT
            rec.perceiver_instance_id,
            NEW.proposition_id,
            NEW.claim_id,
            v_mode,
            v_epistemic_status,
            1.0,
            NEW.dag_node_id,
            jsonb_build_object(
                'acquisition_semantics', 'perceiver-v1',
                'perceiver_instance_id', rec.perceiver_instance_id,
                'perceiver_character_id', rec.perceiver_character_id,
                'origin_character_id', rec.origin_character_id,
                'origin_character_instance_id', rec.origin_instance_id,
                'speaker_id', rec.speaker_id,
                'speaker_role', rec.speaker_role,
                'source_acquisition_mode', rec.source_acquisition_mode,
                'source_key', NEW.source_key,
                'source_kind', NEW.source_kind,
                'observation_id', NEW.observation_id,
                'evidence_origin_preserved', true,
                'confidence_semantics', 'perception_delivery',
                'semantic_confidence_separate', true
            )
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.knowledge_acquisition_event kae
            WHERE kae.instance_id=rec.perceiver_instance_id
              AND kae.claim_id=NEW.claim_id
              AND kae.proposition_id=NEW.proposition_id
        );
    END LOOP;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION aios.project_observation_to_runtime_perceivers() IS
'Projects live chat/observation evidence to exact runtime perceivers bound to the source timeline without changing evidence origin ownership.';

DROP TRIGGER IF EXISTS trg_project_observation_to_runtime_perceivers
ON aios.observation;
CREATE TRIGGER trg_project_observation_to_runtime_perceivers
AFTER INSERT OR UPDATE OF proposition_id, timeline_id, dag_node_id
ON aios.observation
FOR EACH ROW
EXECUTE FUNCTION aios.project_observation_to_runtime_perceivers();

-- Repair existing live-chat acquisition rows that were produced under the old
-- origin-instance == perceiver assumption.  Do not replace the rows: preserve
-- acquisition_id and append the old source-mode meaning to metadata.  Reset
-- processed_at so the ordinary projector recalculates character weights and
-- causes normal belief reconciliation to run.
UPDATE aios.knowledge_acquisition_event kae
SET acquisition_mode=aios.acquisition_mode_for_perceiver(
        dn.kind::text,
        ccr.character_instance_id,
        kae.instance_id
    ),
    confidence=1.0,
    processed_at=NULL,
    meta=COALESCE(kae.meta, '{}'::jsonb) || jsonb_build_object(
        'acquisition_semantics', 'perceiver-v1',
        'perceiver_instance_id', kae.instance_id,
        'perceiver_character_id', ci.character_id,
        'origin_character_id', ccr.origin_character_id,
        'origin_character_instance_id', ccr.character_instance_id,
        'speaker_id', dn.speaker_id,
        'speaker_role', dn.speaker_role::text,
        'source_acquisition_mode', ccr.acquisition_mode,
        'source_key', o.source_key,
        'source_kind', o.source_kind,
        'observation_id', o.observation_id,
        'evidence_origin_preserved', true,
        'confidence_semantics', 'perception_delivery',
        'semantic_confidence_separate', true,
        'legacy_acquisition_repaired', true
    )
FROM aios.observation o,
     aios.dag_node dn,
     aios.claim_context_resolution ccr,
     aios.character_runtime_state rs,
     aios.character_instance ci,
     aios.timeline rt,
     aios.timeline st
WHERE kae.claim_id=o.claim_id
  AND kae.proposition_id=o.proposition_id
  AND dn.node_id=o.dag_node_id
  AND ccr.claim_id=o.claim_id
  AND rs.instance_id=kae.instance_id
  AND rs.source_timeline_id=o.timeline_id
  AND ci.instance_id=kae.instance_id
  AND rt.timeline_id=rs.timeline_id
  AND st.timeline_id=o.timeline_id
  AND dn.kind::text IN ('chat_message','observation')
  AND rt.session_id IS NOT DISTINCT FROM st.session_id
  AND rt.user_name IS NOT DISTINCT FROM st.user_name
  AND rt.scope_key=st.scope_key;

-- Backfill missing listener/perceiver acquisitions. Existing origin evidence is
-- untouched; this adds the missing epistemic edge saying that the runtime
-- instance actually encountered the evidence.
INSERT INTO aios.knowledge_acquisition_event (
    instance_id,
    proposition_id,
    claim_id,
    acquisition_mode,
    epistemic_status,
    confidence,
    dag_node_id,
    meta
)
SELECT
    ci.instance_id,
    o.proposition_id,
    o.claim_id,
    aios.acquisition_mode_for_perceiver(
        dn.kind::text,
        ccr.character_instance_id,
        ci.instance_id
    ),
    CASE upper(COALESCE(ccr.claim_kind, 'UNKNOWN'))
        WHEN 'BELIEF' THEN 'believed'
        WHEN 'MEMORY' THEN 'remembered'
        WHEN 'GOAL' THEN 'intended'
        WHEN 'RULE' THEN 'accepted_rule'
        ELSE 'observed'
    END,
    1.0,
    o.dag_node_id,
    jsonb_build_object(
        'acquisition_semantics', 'perceiver-v1',
        'perceiver_instance_id', ci.instance_id,
        'perceiver_character_id', ci.character_id,
        'origin_character_id', ccr.origin_character_id,
        'origin_character_instance_id', ccr.character_instance_id,
        'speaker_id', dn.speaker_id,
        'speaker_role', dn.speaker_role::text,
        'source_acquisition_mode', ccr.acquisition_mode,
        'source_key', o.source_key,
        'source_kind', o.source_kind,
        'observation_id', o.observation_id,
        'evidence_origin_preserved', true,
        'confidence_semantics', 'perception_delivery',
        'semantic_confidence_separate', true,
        'backfilled_by', 'perceiver-v1'
    )
FROM aios.observation o
JOIN aios.dag_node dn
  ON dn.node_id=o.dag_node_id
LEFT JOIN aios.claim_context_resolution ccr
  ON ccr.claim_id=o.claim_id
JOIN aios.character_runtime_state rs
  ON rs.source_timeline_id=o.timeline_id
JOIN aios.character_instance ci
  ON ci.instance_id=rs.instance_id
JOIN aios.timeline rt
  ON rt.timeline_id=rs.timeline_id
JOIN aios.timeline st
  ON st.timeline_id=o.timeline_id
WHERE dn.kind::text IN ('chat_message','observation')
  AND rt.session_id IS NOT DISTINCT FROM st.session_id
  AND rt.user_name IS NOT DISTINCT FROM st.user_name
  AND rt.scope_key=st.scope_key
  AND NOT EXISTS (
      SELECT 1
      FROM aios.knowledge_acquisition_event kae
      WHERE kae.instance_id=ci.instance_id
        AND kae.claim_id=o.claim_id
        AND kae.proposition_id=o.proposition_id
  );

COMMIT;
