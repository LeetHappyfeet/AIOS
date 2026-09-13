-- AIOS canonical PostgreSQL baseline schema
-- Generated from the working AIOS development database schema on 2026-09-13.
-- This file defines a fresh-install baseline only. Historical migration receipts
-- and one-off repair tables are intentionally excluded. Future schema changes
-- should be applied as immutable migrations layered on top of this baseline.

--
-- PostgreSQL database dump
--


-- Dumped from database version 16.11 (Debian 16.11-1.pgdg13+1)
-- Dumped by pg_dump version 16.11 (Ubuntu 16.11-1.pgdg22.04+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: aios; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA aios;


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA aios;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: actor_type; Type: TYPE; Schema: aios; Owner: -
--

CREATE TYPE aios.actor_type AS ENUM (
    'user',
    'character',
    'agent',
    'system',
    'tool',
    'source'
);


--
-- Name: edge_kind; Type: TYPE; Schema: aios; Owner: -
--

CREATE TYPE aios.edge_kind AS ENUM (
    'next',
    'reply_to',
    'tool_call',
    'tool_result',
    'fork',
    'merge',
    'summary',
    'memory',
    'other'
);


--
-- Name: event_kind; Type: TYPE; Schema: aios; Owner: -
--

CREATE TYPE aios.event_kind AS ENUM (
    'chat_message',
    'heartbeat',
    'status',
    'tool_call',
    'tool_result',
    'memory_inject',
    'system',
    'other',
    'document',
    'paragraph',
    'observation'
);


--
-- Name: node_origin; Type: TYPE; Schema: aios; Owner: -
--

CREATE TYPE aios.node_origin AS ENUM (
    'agent_action',
    'agent_utterance',
    'system_event',
    'informational_ingest'
);


--
-- Name: process_status; Type: TYPE; Schema: aios; Owner: -
--

CREATE TYPE aios.process_status AS ENUM (
    'new',
    'processing',
    'done',
    'error'
);


--
-- Name: acquisition_mode_for_perceiver(text, uuid, uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.acquisition_mode_for_perceiver(p_node_kind text, p_origin_instance_id uuid, p_perceiver_instance_id uuid) RETURNS text
    LANGUAGE sql IMMUTABLE
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


--
-- Name: FUNCTION acquisition_mode_for_perceiver(p_node_kind text, p_origin_instance_id uuid, p_perceiver_instance_id uuid); Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON FUNCTION aios.acquisition_mode_for_perceiver(p_node_kind text, p_origin_instance_id uuid, p_perceiver_instance_id uuid) IS 'Returns acquisition mode from the perceiver perspective. Source transport/origin mode remains provenance metadata.';


--
-- Name: apply_character_belief_policy(uuid, uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.apply_character_belief_policy(p_instance_id uuid, p_atom_id uuid) RETURNS void
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


--
-- Name: assign_semantic_atom(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.assign_semantic_atom() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_atom_key text;
    v_atom_id uuid;
BEGIN
    v_atom_key := CASE
        WHEN NEW.subject_norm IS NULL
         AND NEW.predicate_norm IS NULL
         AND NEW.object_norm IS NULL
            THEN 'raw:' || COALESCE(NEW.canonical_text, '')
        ELSE COALESCE(NEW.subject_norm, '') || E'\x1f'
           || COALESCE(NEW.predicate_norm, '') || E'\x1f'
           || COALESCE(NEW.object_norm, '')
    END;

    INSERT INTO aios.semantic_atom (
        atom_key, subject_norm, predicate_norm, object_norm
    )
    VALUES (
        v_atom_key, NEW.subject_norm, NEW.predicate_norm, NEW.object_norm
    )
    ON CONFLICT (atom_key) DO UPDATE
    SET updated_at=now()
    RETURNING atom_id INTO v_atom_id;

    NEW.atom_id := v_atom_id;
    RETURN NEW;
END;
$$;


--
-- Name: auto_evaluate_acquisition_retention(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.auto_evaluate_acquisition_retention() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM aios.evaluate_acquisition_retention(NEW.acquisition_id);
    RETURN NEW;
END;
$$;


--
-- Name: enforce_frame_interpretation_admission(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.enforce_frame_interpretation_admission() RETURNS trigger
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


--
-- Name: ensure_new_world_objective_timeline(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.ensure_new_world_objective_timeline() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM aios.ensure_world_objective_timeline(NEW.world_id);
    RETURN NEW;
END;
$$;


--
-- Name: ensure_world_objective_timeline(uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.ensure_world_objective_timeline(p_world_id uuid) RETURNS uuid
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_timeline_id uuid;
BEGIN
    SELECT b.objective_timeline_id
      INTO v_timeline_id
      FROM aios.world_timeline_binding b
     WHERE b.world_id = p_world_id;

    IF v_timeline_id IS NOT NULL THEN
        RETURN v_timeline_id;
    END IF;

    SELECT t.timeline_id
      INTO v_timeline_id
      FROM aios.timeline t
     WHERE t.world_id = p_world_id
       AND COALESCE(t.meta->>'timeline_role', '') = 'objective'
     ORDER BY t.created_at, t.timeline_id
     LIMIT 1;

    IF v_timeline_id IS NULL THEN
        INSERT INTO aios.timeline (
            world_id, name, session_id, character_id, user_name,
            scope_key, meta, source_id
        )
        VALUES (
            p_world_id, 'world', NULL, NULL, NULL,
            'world',
            jsonb_build_object(
                'timeline_role', 'objective',
                'shared_world', true,
                'authority', 'world'
            ),
            NULL
        )
        RETURNING timeline_id INTO v_timeline_id;
    END IF;

    INSERT INTO aios.world_timeline_binding (
        world_id, objective_timeline_id, meta
    )
    VALUES (
        p_world_id,
        v_timeline_id,
        jsonb_build_object('source', 'shared-world-topology-v1')
    )
    ON CONFLICT (world_id) DO UPDATE
       SET objective_timeline_id = EXCLUDED.objective_timeline_id,
           meta = aios.world_timeline_binding.meta || EXCLUDED.meta;

    RETURN v_timeline_id;
END;
$$;


--
-- Name: evaluate_acquisition_retention(uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.evaluate_acquisition_retention(p_acquisition_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_status text;
    v_reason text;
    v_instance_id uuid;
    v_proposition_id uuid;
    v_claim_id uuid;
    v_dag_node_id uuid;
    v_duplicate_id uuid;
BEGIN
    SELECT kae.instance_id,kae.proposition_id,kae.claim_id,kae.dag_node_id,
           sea.status,sea.reason
    INTO v_instance_id,v_proposition_id,v_claim_id,v_dag_node_id,v_status,v_reason
    FROM aios.knowledge_acquisition_event kae
    LEFT JOIN aios.semantic_evidence_admission sea
      ON sea.acquisition_id=kae.acquisition_id
    WHERE kae.acquisition_id=p_acquisition_id;

    IF v_instance_id IS NULL THEN
        RETURN;
    END IF;

    -- Only structural parser leakage is automatic extraction trash.
    -- missing_semantic_predicate deliberately stays out of this list: an
    -- incomplete proposition can still be valuable episodic/source evidence
    -- and should remain available for semantic repair.
    IF v_reason IN (
        'internal_frame_reference',
        'serialized_semantic_component'
    ) THEN
        PERFORM aios.set_semantic_retention_state(
            'knowledge_acquisition_event',p_acquisition_id::text,'QUARANTINED',
            'extraction_debris:' || v_reason,
            0.05,0.05,0.0,1.0,
            jsonb_build_object('claim_id',v_claim_id,'admission_reason',v_reason)
        );
        IF v_claim_id IS NOT NULL THEN
            PERFORM aios.set_semantic_retention_state(
                'claim_candidate',v_claim_id::text,'QUARANTINED',
                'extraction_debris:' || v_reason,
                0.05,0.05,0.0,1.0,
                jsonb_build_object('acquisition_id',p_acquisition_id)
            );
        END IF;
        RETURN;
    END IF;

    -- Exact duplicate suppression remains scoped to one character instance,
    -- one proposition, and one DAG generation coordinate. Keep the earliest
    -- ACTIVE representative. semantic_retention_protection() can still veto
    -- the quarantine if current belief state depends on this acquisition.
    SELECT other.acquisition_id
    INTO v_duplicate_id
    FROM aios.knowledge_acquisition_event other
    LEFT JOIN aios.semantic_retention_state rs
      ON rs.artifact_type='knowledge_acquisition_event'
     AND rs.artifact_id=other.acquisition_id::text
    WHERE other.instance_id=v_instance_id
      AND other.proposition_id=v_proposition_id
      AND other.dag_node_id IS NOT DISTINCT FROM v_dag_node_id
      AND other.acquisition_id<>p_acquisition_id
      AND COALESCE(rs.state,'ACTIVE')='ACTIVE'
      AND other.created_at <= (
          SELECT created_at
          FROM aios.knowledge_acquisition_event
          WHERE acquisition_id=p_acquisition_id
      )
    ORDER BY other.created_at,other.acquisition_id
    LIMIT 1;

    IF v_duplicate_id IS NOT NULL THEN
        PERFORM aios.set_semantic_retention_state(
            'knowledge_acquisition_event',p_acquisition_id::text,'QUARANTINED',
            'duplicate_same_event_proposition',
            0.10,0.90,1.0,1.0,
            jsonb_build_object('representative_acquisition_id',v_duplicate_id)
        );
        RETURN;
    END IF;
END;
$$;


--
-- Name: FUNCTION evaluate_acquisition_retention(p_acquisition_id uuid); Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON FUNCTION aios.evaluate_acquisition_retention(p_acquisition_id uuid) IS 'Phase-1 retention evaluator. Quarantines structural parser leakage and safe exact duplicates; incomplete semantic extraction remains active evidence for later repair.';


--
-- Name: link_character_timeline_to_world(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.link_character_timeline_to_world() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_world_timeline_id uuid;
BEGIN
    -- Objective timelines are world-owned and must never point back to themselves.
    IF NEW.character_id IS NULL
       OR COALESCE(NEW.meta->>'timeline_role', '') = 'objective' THEN
        RETURN NEW;
    END IF;

    v_world_timeline_id := aios.ensure_world_objective_timeline(NEW.world_id);

    INSERT INTO aios.character_world_timeline_link (
        character_timeline_id,
        world_id,
        world_timeline_id,
        relation,
        meta
    )
    VALUES (
        NEW.timeline_id,
        NEW.world_id,
        v_world_timeline_id,
        'inhabits',
        jsonb_build_object(
            'source', 'timeline-trigger',
            'character_id', NEW.character_id,
            'session_id', NEW.session_id
        )
    )
    ON CONFLICT (character_timeline_id) DO UPDATE
       SET world_id = EXCLUDED.world_id,
           world_timeline_id = EXCLUDED.world_timeline_id,
           relation = EXCLUDED.relation,
           meta = aios.character_world_timeline_link.meta || EXCLUDED.meta;

    RETURN NEW;
END;
$$;


--
-- Name: mark_belief_scope_dirty(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.mark_belief_scope_dirty() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_instance_id uuid;
    v_character_id text;
    v_scope_key text;
BEGIN
    IF TG_OP='DELETE' THEN
        v_instance_id := OLD.instance_id;
    ELSE
        v_instance_id := NEW.instance_id;
    END IF;

    SELECT ci.character_id
    INTO v_character_id
    FROM aios.character_instance ci
    WHERE ci.instance_id=v_instance_id;

    IF v_character_id IS NOT NULL THEN
        v_scope_key := 'char:' || v_character_id;

        INSERT INTO aios.semantic_scope_projection_state (
            scope_key, scope_kind,
            dirty_version, projected_version,
            status, dirty_at, updated_at
        )
        VALUES (
            v_scope_key, 'character',
            1, 0,
            'dirty', now(), now()
        )
        ON CONFLICT (scope_key) DO UPDATE
        SET scope_kind='character',
            dirty_version=aios.semantic_scope_projection_state.dirty_version + 1,
            status='dirty',
            dirty_at=now(),
            last_error=NULL,
            updated_at=now();
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: mark_character_belief_rdf_dirty(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.mark_character_belief_rdf_dirty() RETURNS trigger
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


--
-- Name: mark_semantic_validation_stale(text, text); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.mark_semantic_validation_stale(p_evidence_type text, p_evidence_key text) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type
          AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    )
    UPDATE aios.semantic_validation_decision v
    SET status='stale', stale_at=COALESCE(v.stale_at, now())
    FROM affected a
    WHERE v.decision_type=a.decision_type
      AND v.decision_key=a.decision_key
      AND v.status <> 'stale';

    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    ), claims AS (
        SELECT DISTINCT v.subject_key
        FROM affected a
        JOIN aios.semantic_validation_decision v
          ON v.decision_type=a.decision_type AND v.decision_key=a.decision_key
        WHERE v.decision_type IN ('semantic_owner','world_assignment','entity_referent')
          AND v.subject_type='claim'
    )
    UPDATE aios.semantic_topology_projection stp
    SET projected_at=NULL,
        updated_at=now(),
        meta=stp.meta || jsonb_build_object('reproject_reason','semantic_validation_stale')
    FROM claims c
    WHERE stp.claim_id::text=c.subject_key
      AND stp.projected_at IS NOT NULL;

    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    ), pairs AS (
        SELECT DISTINCT v.subject_key
        FROM affected a
        JOIN aios.semantic_validation_decision v
          ON v.decision_type=a.decision_type AND v.decision_key=a.decision_key
        WHERE v.decision_type IN ('proposition_relation','event_identity')
    )
    DELETE FROM aios.semantic_neighbor_relation r
    USING pairs p
    WHERE (r.proposition_id::text || ':' || r.neighbor_proposition_id::text)=p.subject_key
       OR (r.neighbor_proposition_id::text || ':' || r.proposition_id::text)=p.subject_key;

    WITH RECURSIVE affected(decision_type, decision_key) AS (
        SELECT d.decision_type, d.decision_key
        FROM aios.semantic_validation_dependency d
        WHERE d.evidence_type=p_evidence_type AND d.evidence_key=p_evidence_key
        UNION
        SELECT child.decision_type, child.decision_key
        FROM affected a
        JOIN aios.semantic_validation_dependency child
          ON child.evidence_type='decision'
         AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
    ), assertions AS (
        SELECT DISTINCT v.subject_key
        FROM affected a
        JOIN aios.semantic_validation_decision v
          ON v.decision_type=a.decision_type AND v.decision_key=a.decision_key
        WHERE v.decision_type='epistemic_promotion'
    )
    UPDATE aios.world_proposition_assertion a
    SET last_checked_at=NULL,
        epistemic_status=CASE
            WHEN a.source_kind='generated_fill' AND a.epistemic_status='corroborated'
            THEN 'provisional'
            ELSE a.epistemic_status
        END,
        updated_at=now()
    FROM assertions s
    WHERE a.assertion_id::text=s.subject_key;
END;
$$;


--
-- Name: project_observation_to_runtime_perceivers(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.project_observation_to_runtime_perceivers() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    rec record;
    v_mode text;
    v_epistemic_status text;
BEGIN
    -- Only event forms that an attached live runtime can directly perceive are
    -- projected automatically. Documents/source material still require an
    -- explicit read/learn acquisition path. The source-head constraint prevents
    -- future/sibling evidence on the source timeline from leaking into /char.
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
        JOIN aios.dag_node source_head
          ON source_head.node_id=rs.source_head_node_id
         AND source_head.timeline_id=NEW.timeline_id
        LEFT JOIN aios.claim_context_resolution ccr
          ON ccr.claim_id=NEW.claim_id
        WHERE rs.source_timeline_id=NEW.timeline_id
          AND dn.event_id <= source_head.event_id
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


--
-- Name: FUNCTION project_observation_to_runtime_perceivers(); Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON FUNCTION aios.project_observation_to_runtime_perceivers() IS 'Projects live chat/observation evidence to exact runtime perceivers bound to the source timeline and not beyond the runtime source head, without changing evidence origin ownership.';


--
-- Name: recompute_semantic_evidence_admission(uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.recompute_semantic_evidence_admission(p_acquisition_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
DECLARE
    v_claim_id uuid;
    v_proposition_id uuid;
    v_raw_text text;
    v_subject_text text;
    v_object_text text;
    v_predicate text;
    v_resolved_subject text;
    v_resolved_object text;
    v_resolution_status text;
    v_discourse_mode text;
    v_epistemic_scope text;
    v_subject_kind text;
    v_object_kind text;
    v_predicate_family text;
    v_claim_kind text;
    v_frame_confidence double precision;
    v_referent_confidence double precision;
    v_context_confidence double precision;
    v_frame_polarity integer;
    v_prop_subject text;
    v_prop_predicate text;
    v_prop_object text;
    v_prop_polarity integer;
    v_status text;
    v_reason text;
    v_confidence double precision;
    v_ambiguous_quoted_participant boolean := false;
    v_internal_frame_reference boolean := false;
    v_serialized_component boolean := false;
    v_negative_scope_unresolved boolean := false;
    v_semantic_self_definition boolean := false;
    v_semantic_role_incompatible boolean := false;
BEGIN
    SELECT kae.claim_id, kae.proposition_id
    INTO v_claim_id, v_proposition_id
    FROM aios.knowledge_acquisition_event kae
    WHERE kae.acquisition_id=p_acquisition_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT p.subject_norm, p.predicate_norm, p.object_norm, p.polarity
    INTO v_prop_subject, v_prop_predicate, v_prop_object, v_prop_polarity
    FROM aios.proposition p
    WHERE p.proposition_id=v_proposition_id;

    IF v_claim_id IS NULL THEN
        v_status := 'active';
        v_reason := 'explicit_nonclaim_acquisition';
        v_confidence := 1.0;
    ELSE
        SELECT
            cc.raw_text,
            sf.subject_text,
            sf.object_text,
            sf.predicate_canonical,
            sf.resolved_subject,
            sf.resolved_object,
            sf.resolution_status,
            sf.discourse_mode,
            sf.frame_confidence,
            sf.referent_confidence,
            sf.polarity,
            ccr.epistemic_scope,
            ccr.subject_kind,
            ccr.object_kind,
            ccr.predicate_family,
            ccr.claim_kind,
            ccr.confidence
        INTO
            v_raw_text,
            v_subject_text,
            v_object_text,
            v_predicate,
            v_resolved_subject,
            v_resolved_object,
            v_resolution_status,
            v_discourse_mode,
            v_frame_confidence,
            v_referent_confidence,
            v_frame_polarity,
            v_epistemic_scope,
            v_subject_kind,
            v_object_kind,
            v_predicate_family,
            v_claim_kind,
            v_context_confidence
        FROM aios.claim_candidate cc
        LEFT JOIN aios.claim_semantic_frame_projection sfp ON sfp.claim_id=cc.claim_id
        LEFT JOIN aios.claim_semantic_frame sf ON sf.frame_id=sfp.primary_frame_id
        LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
        WHERE cc.claim_id=v_claim_id;

        IF NOT FOUND OR v_predicate IS NULL THEN
            v_status := 'suppressed';
            v_reason := 'missing_semantic_predicate';
            v_confidence := 0.0;
        ELSE
            v_internal_frame_reference :=
                COALESCE(v_prop_subject, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+'
                OR COALESCE(v_prop_predicate, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+'
                OR COALESCE(v_prop_object, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+'
                OR COALESCE(v_resolved_subject, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+'
                OR COALESCE(v_resolved_object, '') ~* '(^|[^[:alnum:]_])frame:[0-9]+';

            v_serialized_component :=
                position('|' in COALESCE(v_prop_subject, '')) > 0
                OR position('|' in COALESCE(v_prop_predicate, '')) > 0
                OR position('|' in COALESCE(v_prop_object, '')) > 0
                OR position('|' in COALESCE(v_resolved_subject, '')) > 0
                OR position('|' in COALESCE(v_resolved_object, '')) > 0;

            v_negative_scope_unresolved :=
                COALESCE(v_prop_polarity, 1) = -1
                AND (
                    v_frame_polarity IS DISTINCT FROM -1
                    OR COALESCE(v_resolution_status, 'partial') <> 'resolved'
                );

            v_semantic_self_definition :=
                COALESCE(v_prop_predicate, '')='be_definition_of'
                AND NULLIF(btrim(COALESCE(v_prop_subject, '')), '') IS NOT NULL
                AND lower(btrim(v_prop_subject))=lower(btrim(COALESCE(v_prop_object, '')));

            -- Reject only role combinations the resolver classified strongly
            -- enough to prove incompatible. UNKNOWN remains unresolved by later
            -- evidence rather than being guessed here.
            v_semantic_role_incompatible :=
                CASE COALESCE(v_predicate_family, 'UNKNOWN')
                    WHEN 'SPATIAL' THEN
                        COALESCE(v_object_kind, 'UNKNOWN') NOT IN ('LOCATION','UNKNOWN')
                    WHEN 'TEMPORAL' THEN
                        COALESCE(v_object_kind, 'UNKNOWN') NOT IN ('TIME','UNKNOWN')
                    WHEN 'SOCIAL' THEN
                        COALESCE(v_object_kind, 'UNKNOWN') NOT IN ('PERSON','RELATIONSHIP','UNKNOWN')
                    WHEN 'MEMBERSHIP' THEN
                        COALESCE(v_object_kind, 'UNKNOWN') NOT IN ('ORGANIZATION','UNKNOWN')
                    WHEN 'POSSESSION' THEN
                        COALESCE(v_subject_kind, 'UNKNOWN') IN ('TIME','EVENT','MEMORY','RULE','GOAL')
                    WHEN 'ACTION' THEN
                        COALESCE(v_subject_kind, 'UNKNOWN') IN ('TIME','RULE','GOAL','QUANTITY')
                    WHEN 'IDENTITY' THEN
                        COALESCE(v_prop_predicate, '')='be_definition_of'
                        AND COALESCE(v_object_kind, 'UNKNOWN') IN (
                            'LOCATION','TIME','EVENT','ACTION','QUANTITY','MEMORY','RELATIONSHIP','GOAL','RULE'
                        )
                    ELSE false
                END;

            v_ambiguous_quoted_participant :=
                COALESCE(v_discourse_mode, 'narrated_observation')='narrated_observation'
                AND btrim(COALESCE(v_raw_text, '')) ~ '^["“].*["”][.!?]?$'
                AND (
                    lower(COALESCE(v_subject_text, '')) ~ '(^|[[:space:]])(i|me|my|mine|we|us|our|ours|you|your|yours)([[:space:]]|$)'
                    OR lower(COALESCE(v_object_text, '')) ~ '(^|[[:space:]])(i|me|my|mine|we|us|our|ours|you|your|yours)([[:space:]]|$)'
                );

            IF v_internal_frame_reference THEN
                v_status := 'unresolved';
                v_reason := 'internal_frame_reference';
            ELSIF v_serialized_component THEN
                v_status := 'unresolved';
                v_reason := 'serialized_semantic_component';
            ELSIF v_negative_scope_unresolved THEN
                v_status := 'unresolved';
                v_reason := 'negative_polarity_scope_unresolved';
            ELSIF v_semantic_self_definition THEN
                v_status := 'unresolved';
                v_reason := 'semantic_self_definition';
            ELSIF v_semantic_role_incompatible THEN
                v_status := 'unresolved';
                v_reason := 'semantic_role_incompatible';
            ELSIF v_ambiguous_quoted_participant THEN
                v_status := 'unresolved';
                v_reason := 'quoted_local_discourse_unresolved';
            ELSIF COALESCE(v_resolution_status, 'partial') <> 'resolved' THEN
                v_status := 'unresolved';
                v_reason := 'semantic_frame_partial';
            ELSIF COALESCE(v_referent_confidence, 0.0) < 0.55 THEN
                v_status := 'unresolved';
                v_reason := 'referent_confidence_below_threshold';
            ELSIF COALESCE(v_frame_confidence, 0.0) < 0.55 THEN
                v_status := 'unresolved';
                v_reason := 'frame_confidence_below_threshold';
            ELSE
                v_status := 'active';
                v_reason := 'admitted';
            END IF;

            v_confidence := LEAST(
                1.0,
                GREATEST(
                    0.0,
                    LEAST(
                        COALESCE(v_frame_confidence, 0.0),
                        COALESCE(v_context_confidence, 1.0)
                    )
                )
            );
        END IF;
    END IF;

    INSERT INTO aios.semantic_evidence_admission (
        acquisition_id, status, reason, confidence, resolver_version, meta,
        resolved_at, updated_at
    )
    VALUES (
        p_acquisition_id,
        v_status,
        v_reason,
        v_confidence,
        'semantic-admission-v3',
        jsonb_build_object(
            'claim_id', v_claim_id,
            'proposition_id', v_proposition_id,
            'epistemic_scope', v_epistemic_scope,
            'claim_kind', v_claim_kind,
            'subject_kind', v_subject_kind,
            'object_kind', v_object_kind,
            'predicate_family', v_predicate_family,
            'discourse_mode', v_discourse_mode,
            'ambiguous_quoted_participant', v_ambiguous_quoted_participant,
            'internal_frame_reference', v_internal_frame_reference,
            'serialized_semantic_component', v_serialized_component,
            'negative_polarity_scope_unresolved', v_negative_scope_unresolved,
            'semantic_self_definition', v_semantic_self_definition,
            'semantic_role_incompatible', v_semantic_role_incompatible,
            'frame_polarity', v_frame_polarity,
            'proposition_polarity', v_prop_polarity,
            'structural_firewall', true,
            'typed_role_firewall', true
        ),
        now(),
        now()
    )
    ON CONFLICT (acquisition_id) DO UPDATE
    SET status=EXCLUDED.status,
        reason=EXCLUDED.reason,
        confidence=EXCLUDED.confidence,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,
        resolved_at=now(),
        updated_at=now();
END;
$_$;


--
-- Name: reconcile_belief_descendants(uuid, uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.reconcile_belief_descendants(p_source_instance_id uuid, p_atom_id uuid) RETURNS void
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


--
-- Name: reconcile_character_belief_atom(uuid, uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.reconcile_character_belief_atom(p_instance_id uuid, p_atom_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM aios.reconcile_character_belief_atom_generic_v1(p_instance_id, p_atom_id);
    PERFORM aios.apply_character_belief_policy(p_instance_id, p_atom_id);
END;
$$;


--
-- Name: reconcile_character_belief_atom_generic_v1(uuid, uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.reconcile_character_belief_atom_generic_v1(p_instance_id uuid, p_atom_id uuid) RETURNS void
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


--
-- Name: reconcile_world_memory_atom(uuid, uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.reconcile_world_memory_atom(p_world_id uuid, p_atom_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_positive_support double precision := 0.0;
    v_negative_support double precision := 0.0;
    v_evidence_count integer := 0;
    v_independent_count integer := 0;
    v_stance text;
    v_state_confidence double precision := 0.0;
    v_preferred_proposition_id uuid;
    v_preferred_assertion_id uuid;
    v_resolved_through_node_id uuid;
BEGIN
    WITH evidence AS (
        SELECT a.assertion_id,a.proposition_id,p.polarity,
               LEAST(0.999999,GREATEST(0.0,COALESCE(a.confidence,0.0))) AS weight,
               COALESCE(a.source_kind,'unknown') || ':' || a.assertion_id::text AS correlation_key,
               a.generated_at_node_id,a.updated_at
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        LEFT JOIN aios.semantic_retention_state rs
          ON rs.artifact_type='world_proposition_assertion'
         AND rs.artifact_id=a.assertion_id::text
        WHERE a.world_id=p_world_id
          AND p.atom_id=p_atom_id
          AND a.epistemic_status NOT IN ('rejected','superseded')
          AND COALESCE(rs.state,'ACTIVE')='ACTIVE'
    ), correlated AS (
        SELECT polarity,correlation_key,MAX(weight) AS weight
        FROM evidence GROUP BY polarity,correlation_key
    )
    SELECT
        COALESCE(1.0-exp(SUM(ln(1.0-weight)) FILTER (WHERE polarity=1)),0.0),
        COALESCE(1.0-exp(SUM(ln(1.0-weight)) FILTER (WHERE polarity=-1)),0.0),
        (SELECT COUNT(*) FROM evidence),COUNT(*)
    INTO v_positive_support,v_negative_support,v_evidence_count,v_independent_count
    FROM correlated;

    IF v_evidence_count=0 THEN
        DELETE FROM aios.world_memory_state
        WHERE world_id=p_world_id AND atom_id=p_atom_id;
        RETURN;
    END IF;

    IF v_positive_support>=0.60 AND (v_positive_support-v_negative_support)>=0.15 THEN
        v_stance:='positive';
    ELSIF v_negative_support>=0.60 AND (v_negative_support-v_positive_support)>=0.15 THEN
        v_stance:='negative';
    ELSE
        v_stance:='unresolved';
    END IF;
    v_state_confidence:=LEAST(1.0,GREATEST(0.0,abs(v_positive_support-v_negative_support)));

    SELECT a.proposition_id,a.assertion_id,a.generated_at_node_id
    INTO v_preferred_proposition_id,v_preferred_assertion_id,v_resolved_through_node_id
    FROM aios.world_proposition_assertion a
    JOIN aios.proposition p ON p.proposition_id=a.proposition_id
    LEFT JOIN aios.semantic_retention_state rs
      ON rs.artifact_type='world_proposition_assertion'
     AND rs.artifact_id=a.assertion_id::text
    WHERE a.world_id=p_world_id
      AND p.atom_id=p_atom_id
      AND a.epistemic_status NOT IN ('rejected','superseded')
      AND COALESCE(rs.state,'ACTIVE')='ACTIVE'
    ORDER BY
        CASE
            WHEN v_stance='positive' AND p.polarity=1 THEN 0
            WHEN v_stance='negative' AND p.polarity=-1 THEN 0
            WHEN v_stance='unresolved' THEN 0 ELSE 1
        END,
        a.confidence DESC,a.updated_at DESC,a.assertion_id
    LIMIT 1;

    INSERT INTO aios.world_memory_state (
        world_id,atom_id,stance,positive_support,negative_support,state_confidence,
        preferred_proposition_id,preferred_assertion_id,evidence_count,
        independent_evidence_count,resolved_through_node_id,resolver_version,meta,
        resolved_at,updated_at
    ) VALUES (
        p_world_id,p_atom_id,v_stance,v_positive_support,v_negative_support,
        v_state_confidence,v_preferred_proposition_id,v_preferred_assertion_id,
        v_evidence_count,v_independent_count,v_resolved_through_node_id,
        'memory-reconciliation-v1',
        jsonb_build_object(
            'path','world','authority_boundary','world_proposition_assertion',
            'evidence_topology_preserved',true,'retention_filter','semantic-retention-v1',
            'accept_support',0.60,'decision_margin',0.15
        ),now(),now()
    )
    ON CONFLICT (world_id,atom_id) DO UPDATE
    SET stance=EXCLUDED.stance,
        positive_support=EXCLUDED.positive_support,
        negative_support=EXCLUDED.negative_support,
        state_confidence=EXCLUDED.state_confidence,
        preferred_proposition_id=EXCLUDED.preferred_proposition_id,
        preferred_assertion_id=EXCLUDED.preferred_assertion_id,
        evidence_count=EXCLUDED.evidence_count,
        independent_evidence_count=EXCLUDED.independent_evidence_count,
        resolved_through_node_id=EXCLUDED.resolved_through_node_id,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,resolved_at=now(),updated_at=now();
END;
$$;


--
-- Name: reconsider_unresolved_topology_from_neighbor(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.reconsider_unresolved_topology_from_neighbor() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE aios.semantic_topology_projection stp
    SET projected_at=NULL,
        updated_at=now(),
        meta=stp.meta || jsonb_build_object(
            'reproject_reason', 'semantic_neighbor_evidence',
            'neighbor_embedding_version', NEW.embedding_version
        )
    FROM aios.observation o
    WHERE stp.claim_id=o.claim_id
      AND stp.projected_at IS NOT NULL
      AND stp.scope_key=('unresolved:' || o.claim_id::text)
      AND o.proposition_id IN (NEW.proposition_id, NEW.neighbor_proposition_id);
    RETURN NEW;
END;
$$;


--
-- Name: refresh_admission_for_claim(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_admission_for_claim() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_claim_id uuid;
    rec record;
BEGIN
    IF TG_OP='DELETE' THEN
        v_claim_id := OLD.claim_id;
    ELSE
        v_claim_id := NEW.claim_id;
    END IF;
    FOR rec IN
        SELECT acquisition_id
        FROM aios.knowledge_acquisition_event
        WHERE claim_id=v_claim_id
    LOOP
        PERFORM aios.recompute_semantic_evidence_admission(rec.acquisition_id);
    END LOOP;
    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: refresh_admission_from_acquisition(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_admission_from_acquisition() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM aios.recompute_semantic_evidence_admission(NEW.acquisition_id);
    RETURN NEW;
END;
$$;


--
-- Name: refresh_belief_from_acquisition_topology(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_belief_from_acquisition_topology() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_instance_id uuid;
    v_atom_id uuid;
BEGIN
    SELECT kae.instance_id, p.atom_id
    INTO v_instance_id, v_atom_id
    FROM aios.knowledge_acquisition_event kae
    JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
    WHERE kae.acquisition_id=NEW.acquisition_id;

    IF v_instance_id IS NOT NULL AND v_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_belief_descendants(v_instance_id, v_atom_id);
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: refresh_belief_from_admission(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_belief_from_admission() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_acquisition_id uuid;
    v_instance_id uuid;
    v_atom_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        v_acquisition_id := OLD.acquisition_id;
    ELSE
        v_acquisition_id := NEW.acquisition_id;
    END IF;

    SELECT kae.instance_id, p.atom_id
    INTO v_instance_id, v_atom_id
    FROM aios.knowledge_acquisition_event kae
    JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
    WHERE kae.acquisition_id=v_acquisition_id;

    IF v_instance_id IS NOT NULL AND v_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_belief_descendants(v_instance_id, v_atom_id);
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: refresh_belief_from_character_evidence(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_belief_from_character_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_instance_id uuid;
    v_proposition_id uuid;
    v_atom_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        v_instance_id := OLD.instance_id;
        v_proposition_id := OLD.proposition_id;
    ELSE
        v_instance_id := NEW.instance_id;
        v_proposition_id := NEW.proposition_id;
    END IF;

    SELECT atom_id INTO v_atom_id
    FROM aios.proposition
    WHERE proposition_id=v_proposition_id;

    IF v_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_belief_descendants(v_instance_id, v_atom_id);
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: refresh_belief_from_ingest_supersession(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_belief_from_ingest_supersession() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    rec record;
BEGIN
    IF OLD.superseded_at IS NOT DISTINCT FROM NEW.superseded_at THEN
        RETURN NEW;
    END IF;

    FOR rec IN
        SELECT DISTINCT kae.instance_id, p.atom_id
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
        JOIN aios.dag_node dn ON dn.node_id=kae.dag_node_id
        WHERE dn.event_id=NEW.event_id
          AND p.atom_id IS NOT NULL
    LOOP
        PERFORM aios.reconcile_belief_descendants(rec.instance_id, rec.atom_id);
    END LOOP;

    RETURN NEW;
END;
$$;


--
-- Name: refresh_message_enrichment_cursor(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_message_enrichment_cursor() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    target_commit record;
BEGIN
    IF NEW.projected_at IS NULL THEN
        RETURN NEW;
    END IF;

    FOR target_commit IN
        SELECT DISTINCT
            mcc.commit_id,
            mcc.instance_id,
            mcc.node_id,
            mcc.event_id,
            mcc.character_id
        FROM aios.message_cognitive_commit mcc
        WHERE
            (NEW.claim_id IS NOT NULL AND EXISTS (
                SELECT 1
                FROM aios.claim_candidate cc
                JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
                JOIN aios.document_section ds ON ds.section_id=es.section_id
                WHERE cc.claim_id=NEW.claim_id
                  AND ds.node_id=mcc.node_id
            ))
            OR
            (NEW.acquisition_id IS NOT NULL AND EXISTS (
                SELECT 1
                FROM aios.knowledge_acquisition_event kae
                JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
                JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
                JOIN aios.document_section ds ON ds.section_id=es.section_id
                WHERE kae.acquisition_id=NEW.acquisition_id
                  AND kae.instance_id=mcc.instance_id
                  AND ds.node_id=mcc.node_id
            ))
    LOOP
        -- Every evidence claim for this message must be normalized and have its
        -- claim topology projected before archaeology can be called complete.
        IF EXISTS (
            SELECT 1
            FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            WHERE ds.node_id=target_commit.node_id
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM aios.observation o
                      WHERE o.claim_id=cc.claim_id
                  )
                  OR NOT EXISTS (
                      SELECT 1
                      FROM aios.semantic_topology_projection stp
                      WHERE stp.claim_id=cc.claim_id
                        AND stp.resolver_version='semantic-topology-v1'
                        AND stp.projected_at IS NOT NULL
                  )
              )
        ) THEN
            CONTINUE;
        END IF;

        -- Any character-owned cognition from this source must also have a
        -- processed acquisition and acquisition topology for this exact runtime.
        IF EXISTS (
            SELECT 1
            FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
            WHERE ds.node_id=target_commit.node_id
              AND ccr.epistemic_scope='character'
              AND ccr.origin_character_id=target_commit.character_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM aios.knowledge_acquisition_event kae
                  WHERE kae.instance_id=target_commit.instance_id
                    AND kae.claim_id=cc.claim_id
                    AND kae.processed_at IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM aios.semantic_topology_projection astp
                        WHERE astp.acquisition_id=kae.acquisition_id
                          AND astp.resolver_version='semantic-topology-v1'
                          AND astp.projected_at IS NOT NULL
                    )
              )
        ) THEN
            CONTINUE;
        END IF;

        UPDATE aios.message_cognitive_commit
        SET enrichment_completed_at=COALESCE(enrichment_completed_at, now())
        WHERE commit_id=target_commit.commit_id;

        UPDATE aios.character_hud_readiness
        SET enrichment_ready_node_id=target_commit.node_id,
            enrichment_ready_event_id=target_commit.event_id,
            updated_at=now()
        WHERE instance_id=target_commit.instance_id
          AND (
              enrichment_ready_event_id IS NULL
              OR target_commit.event_id IS NULL
              OR enrichment_ready_event_id <= target_commit.event_id
          );
    END LOOP;

    RETURN NEW;
END;
$$;


--
-- Name: refresh_message_enrichment_from_projection(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_message_enrichment_from_projection() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    source_node_id uuid;
BEGIN
    IF NEW.projected_at IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.claim_id IS NOT NULL THEN
        SELECT ds.node_id
        INTO source_node_id
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es
          ON es.sentence_id = cc.sentence_id
        JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        WHERE cc.claim_id = NEW.claim_id
        LIMIT 1;
    ELSIF NEW.acquisition_id IS NOT NULL THEN
        SELECT ds.node_id
        INTO source_node_id
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.claim_candidate cc
          ON cc.claim_id = kae.claim_id
        JOIN aios.extracted_sentence es
          ON es.sentence_id = cc.sentence_id
        JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        WHERE kae.acquisition_id = NEW.acquisition_id
        LIMIT 1;
    END IF;

    IF source_node_id IS NOT NULL THEN
        PERFORM aios.refresh_message_enrichment_readiness(source_node_id);
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: refresh_message_enrichment_from_section(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_message_enrichment_from_section() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.claims_extracted_at IS NOT NULL
       AND OLD.claims_extracted_at IS DISTINCT FROM NEW.claims_extracted_at
    THEN
        PERFORM aios.refresh_message_enrichment_readiness(NEW.node_id);
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: refresh_message_enrichment_readiness(uuid); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_message_enrichment_readiness(p_node_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    commit_row record;
    total_claims bigint;
    claim_topology_ready bigint;
    acquisition_required bigint;
    acquisition_topology_ready bigint;
BEGIN
    IF p_node_id IS NULL THEN
        RETURN;
    END IF;

    -- Extraction completion is the boundary at which a zero-claim message can
    -- be considered fully enriched. For non-empty messages the topology counts
    -- below remain the actual completion barrier.
    IF NOT EXISTS (
        SELECT 1
        FROM aios.document_section ds
        WHERE ds.node_id = p_node_id
          AND ds.claims_extracted_at IS NOT NULL
    ) THEN
        RETURN;
    END IF;

    FOR commit_row IN
        SELECT mcc.instance_id, mcc.node_id, mcc.event_id, ci.character_id
        FROM aios.message_cognitive_commit mcc
        JOIN aios.character_instance ci
          ON ci.instance_id = mcc.instance_id
        WHERE mcc.node_id = p_node_id
          AND mcc.enrichment_completed_at IS NULL
    LOOP
        SELECT
            count(DISTINCT cc.claim_id),
            count(DISTINCT CASE
                WHEN stp.projected_at IS NOT NULL THEN cc.claim_id
            END),
            count(DISTINCT CASE
                WHEN ccr.epistemic_scope = 'character'
                 AND ccr.origin_character_id = commit_row.character_id
                THEN cc.claim_id
            END),
            count(DISTINCT CASE
                WHEN ccr.epistemic_scope = 'character'
                 AND ccr.origin_character_id = commit_row.character_id
                 AND ccr.character_instance_id = commit_row.instance_id
                 AND kae.processed_at IS NOT NULL
                 AND astp.projected_at IS NOT NULL
                THEN cc.claim_id
            END)
        INTO
            total_claims,
            claim_topology_ready,
            acquisition_required,
            acquisition_topology_ready
        FROM aios.document_section ds
        LEFT JOIN aios.extracted_sentence es
          ON es.section_id = ds.section_id
        LEFT JOIN aios.claim_candidate cc
          ON cc.sentence_id = es.sentence_id
        LEFT JOIN aios.claim_context_resolution ccr
          ON ccr.claim_id = cc.claim_id
        LEFT JOIN aios.knowledge_acquisition_event kae
          ON kae.claim_id = cc.claim_id
         AND kae.instance_id = commit_row.instance_id
        LEFT JOIN aios.semantic_topology_projection stp
          ON stp.claim_id = cc.claim_id
         AND stp.resolver_version = 'semantic-topology-v1'
        LEFT JOIN aios.semantic_topology_projection astp
          ON astp.acquisition_id = kae.acquisition_id
         AND astp.resolver_version = 'semantic-topology-v1'
        WHERE ds.node_id = p_node_id;

        IF COALESCE(claim_topology_ready, 0) = COALESCE(total_claims, 0)
           AND COALESCE(acquisition_topology_ready, 0) = COALESCE(acquisition_required, 0)
        THEN
            UPDATE aios.message_cognitive_commit
            SET enrichment_completed_at = COALESCE(enrichment_completed_at, now())
            WHERE instance_id = commit_row.instance_id
              AND node_id = commit_row.node_id;

            UPDATE aios.character_hud_readiness
            SET enrichment_ready_node_id = commit_row.node_id,
                enrichment_ready_event_id = commit_row.event_id,
                updated_at = now()
            WHERE instance_id = commit_row.instance_id;
        END IF;
    END LOOP;
END;
$$;


--
-- Name: refresh_world_memory_from_assertion(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.refresh_world_memory_from_assertion() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_old_atom_id uuid;
    v_new_atom_id uuid;
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN
        SELECT atom_id INTO v_old_atom_id
        FROM aios.proposition
        WHERE proposition_id=OLD.proposition_id;
    END IF;

    IF TG_OP IN ('INSERT','UPDATE') THEN
        SELECT atom_id INTO v_new_atom_id
        FROM aios.proposition
        WHERE proposition_id=NEW.proposition_id;
    END IF;

    -- An assertion can theoretically move between worlds/atoms during repair.
    -- Reconcile the old coordinate first so no stale current state is stranded.
    IF TG_OP='DELETE' THEN
        IF v_old_atom_id IS NOT NULL THEN
            PERFORM aios.reconcile_world_memory_atom(OLD.world_id, v_old_atom_id);
        END IF;
        DELETE FROM aios.memory_reconciliation_receipt
        WHERE assertion_id=OLD.assertion_id;
        RETURN OLD;
    END IF;

    IF TG_OP='UPDATE'
       AND v_old_atom_id IS NOT NULL
       AND (OLD.world_id IS DISTINCT FROM NEW.world_id
            OR v_old_atom_id IS DISTINCT FROM v_new_atom_id) THEN
        PERFORM aios.reconcile_world_memory_atom(OLD.world_id, v_old_atom_id);
    END IF;

    IF v_new_atom_id IS NOT NULL THEN
        PERFORM aios.reconcile_world_memory_atom(NEW.world_id, v_new_atom_id);

        INSERT INTO aios.memory_reconciliation_receipt (
            claim_id, assertion_id, proposition_id, atom_id, path, scope_key,
            outcome, resolver_version, meta, reconciled_at, updated_at
        )
        VALUES (
            NULL,
            NEW.assertion_id,
            NEW.proposition_id,
            v_new_atom_id,
            'world',
            'world:' || NEW.world_id::text,
            CASE
                WHEN NEW.epistemic_status IN ('rejected','superseded')
                    THEN 'world_assertion_excluded'
                ELSE 'world_memory_reconciled'
            END,
            'memory-reconciliation-v1',
            jsonb_build_object(
                'authority_boundary', 'world_proposition_assertion',
                'epistemic_status', NEW.epistemic_status,
                'source_kind', NEW.source_kind,
                'confidence', NEW.confidence,
                'evidence_topology_preserved', true
            ),
            now(),
            now()
        )
        ON CONFLICT (assertion_id) DO UPDATE
        SET proposition_id=EXCLUDED.proposition_id,
            atom_id=EXCLUDED.atom_id,
            path=EXCLUDED.path,
            scope_key=EXCLUDED.scope_key,
            outcome=EXCLUDED.outcome,
            resolver_version=EXCLUDED.resolver_version,
            meta=EXCLUDED.meta,
            reconciled_at=now(),
            updated_at=now();
    END IF;

    RETURN NEW;
END;
$$;


--
-- Name: reject_nonexclusive_definition_conflict(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.reject_nonexclusive_definition_conflict() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_predicate_a text;
    v_predicate_b text;
BEGIN
    IF NEW.conflict_type <> 'exclusive_object' THEN
        RETURN NEW;
    END IF;

    SELECT predicate_norm INTO v_predicate_a
    FROM aios.proposition
    WHERE proposition_id=NEW.proposition_a_id;

    SELECT predicate_norm INTO v_predicate_b
    FROM aios.proposition
    WHERE proposition_id=NEW.proposition_b_id;

    IF v_predicate_a='be_definition_of' OR v_predicate_b='be_definition_of' THEN
        RETURN NULL;
    END IF;

    RETURN NEW;
END;
$$;


--
-- Name: reject_retention_event_mutation(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.reject_retention_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'aios.retention_event is append-only';
END;
$$;


--
-- Name: route_normalized_memory_evidence(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.route_normalized_memory_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_atom_id uuid;
    v_scope text;
    v_world_id uuid;
    v_character_id text;
    v_instance_id uuid;
    v_source_id text;
    v_path text;
    v_scope_key text;
    v_outcome text;
BEGIN
    SELECT p.atom_id
    INTO v_atom_id
    FROM aios.proposition p
    WHERE p.proposition_id=NEW.proposition_id;

    SELECT
        ccr.epistemic_scope,
        ccr.world_id,
        ccr.origin_character_id,
        ccr.character_instance_id,
        ccr.source_id
    INTO
        v_scope,
        v_world_id,
        v_character_id,
        v_instance_id,
        v_source_id
    FROM aios.claim_context_resolution ccr
    WHERE ccr.claim_id=NEW.claim_id;

    IF v_atom_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF v_scope='character' AND v_instance_id IS NOT NULL THEN
        v_path := 'char';
        v_scope_key := 'char:' || COALESCE(v_character_id, v_instance_id::text);
        v_outcome := 'character_routed_to_acquisition_boundary';
    ELSE
        v_path := 'evidence';
        v_scope_key := CASE
            WHEN v_source_id IS NOT NULL THEN 'source:' || v_source_id
            WHEN v_scope='narrative' AND v_world_id IS NOT NULL
                THEN 'world:' || v_world_id::text || ':narrative-evidence'
            WHEN v_world_id IS NOT NULL
                THEN 'world:' || v_world_id::text || ':evidence'
            ELSE 'claim:' || NEW.claim_id::text
        END;
        v_outcome := 'evidence_only';
    END IF;

    INSERT INTO aios.memory_reconciliation_receipt (
        claim_id, assertion_id, proposition_id, atom_id, path, scope_key,
        outcome, resolver_version, meta, reconciled_at, updated_at
    )
    VALUES (
        NEW.claim_id,
        NULL,
        NEW.proposition_id,
        v_atom_id,
        v_path,
        v_scope_key,
        v_outcome,
        'memory-reconciliation-v1',
        jsonb_build_object(
            'epistemic_scope', v_scope,
            'world_id', v_world_id,
            'character_id', v_character_id,
            'character_instance_id', v_instance_id,
            'world_promotion', false,
            'evidence_topology_preserved', true
        ),
        now(),
        now()
    )
    ON CONFLICT (claim_id) DO UPDATE
    SET proposition_id=EXCLUDED.proposition_id,
        atom_id=EXCLUDED.atom_id,
        path=EXCLUDED.path,
        scope_key=EXCLUDED.scope_key,
        outcome=EXCLUDED.outcome,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,
        reconciled_at=now(),
        updated_at=now();

    RETURN NEW;
END;
$$;


--
-- Name: semantic_frame_policy_v3(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_frame_policy_v3() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
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
$_$;


--
-- Name: semantic_retention_protection(text, text); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_retention_protection(p_artifact_type text, p_artifact_id text) RETURNS TABLE(protected boolean, reason text, scope_key text)
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_acquisition_id uuid;
    v_assertion_id uuid;
    v_instance_id uuid;
    v_proposition_id uuid;
    v_atom_id uuid;
    v_world_id uuid;
    v_other_active integer;
BEGIN
    protected := false;
    reason := NULL;
    scope_key := NULL;

    IF p_artifact_type='knowledge_acquisition_event' THEN
        BEGIN
            v_acquisition_id := p_artifact_id::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN NEXT;
            RETURN;
        END;

        SELECT kae.instance_id, kae.proposition_id, p.atom_id,
               'char:' || ci.character_id
        INTO v_instance_id, v_proposition_id, v_atom_id, scope_key
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
        JOIN aios.character_instance ci ON ci.instance_id=kae.instance_id
        WHERE kae.acquisition_id=v_acquisition_id;

        IF v_instance_id IS NULL OR v_atom_id IS NULL THEN
            RETURN NEXT;
            RETURN;
        END IF;

        IF EXISTS (
            SELECT 1
            FROM aios.character_belief_state bs
            WHERE bs.instance_id=v_instance_id
              AND bs.atom_id=v_atom_id
              AND bs.preferred_proposition_id=v_proposition_id
        ) THEN
            SELECT COUNT(*)::integer
            INTO v_other_active
            FROM aios.knowledge_acquisition_event other
            JOIN aios.proposition op ON op.proposition_id=other.proposition_id
            JOIN aios.semantic_evidence_admission sea
              ON sea.acquisition_id=other.acquisition_id
             AND sea.status='active'
            LEFT JOIN aios.semantic_retention_state rs
              ON rs.artifact_type='knowledge_acquisition_event'
             AND rs.artifact_id=other.acquisition_id::text
            WHERE other.instance_id=v_instance_id
              AND op.atom_id=v_atom_id
              AND other.acquisition_id<>v_acquisition_id
              AND COALESCE(rs.state,'ACTIVE')='ACTIVE';

            IF COALESCE(v_other_active,0)=0 THEN
                protected := true;
                reason := 'sole_current_char_support';
            END IF;
        END IF;

        RETURN NEXT;
        RETURN;
    END IF;

    IF p_artifact_type='world_proposition_assertion' THEN
        BEGIN
            v_assertion_id := p_artifact_id::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN NEXT;
            RETURN;
        END;

        SELECT a.world_id, p.atom_id, 'world:' || a.world_id::text
        INTO v_world_id, v_atom_id, scope_key
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        WHERE a.assertion_id=v_assertion_id;

        IF EXISTS (
            SELECT 1
            FROM aios.world_memory_state wms
            WHERE wms.world_id=v_world_id
              AND wms.atom_id=v_atom_id
              AND wms.preferred_assertion_id=v_assertion_id
        ) THEN
            protected := true;
            reason := 'preferred_current_world_support';
        END IF;

        RETURN NEXT;
        RETURN;
    END IF;

    RETURN NEXT;
END;
$$;


--
-- Name: semantic_validation_character_alias_trigger(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_validation_character_alias_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM aios.mark_semantic_validation_stale('character_identity', lower(trim(NEW.alias)));
    RETURN NEW;
END;
$$;


--
-- Name: semantic_validation_character_identity_trigger(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_validation_character_identity_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM aios.mark_semantic_validation_stale('character_identity', lower(trim(NEW.character_id)));
    IF NEW.display_name IS NOT NULL THEN
        PERFORM aios.mark_semantic_validation_stale('character_identity', lower(trim(NEW.display_name)));
    END IF;
    IF NEW.canonical_name IS NOT NULL THEN
        PERFORM aios.mark_semantic_validation_stale('character_identity', lower(trim(NEW.canonical_name)));
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: semantic_validation_conflict_trigger(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_validation_conflict_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    pair_forward text;
    pair_reverse text;
BEGIN
    pair_forward := NEW.proposition_a_id::text || ':' || NEW.proposition_b_id::text;
    pair_reverse := NEW.proposition_b_id::text || ':' || NEW.proposition_a_id::text;
    PERFORM aios.mark_semantic_validation_stale('proposition_conflict', NEW.proposition_a_id::text);
    PERFORM aios.mark_semantic_validation_stale('proposition_conflict', NEW.proposition_b_id::text);
    PERFORM aios.mark_semantic_validation_stale('proposition_conflict', pair_forward);
    PERFORM aios.mark_semantic_validation_stale('proposition_conflict', pair_reverse);
    RETURN NEW;
END;
$$;


--
-- Name: semantic_validation_context_trigger(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_validation_context_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM aios.mark_semantic_validation_stale('claim_context', NEW.claim_id::text);
    RETURN NEW;
END;
$$;


--
-- Name: semantic_validation_decision_trigger(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_validation_decision_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        PERFORM aios.mark_semantic_validation_stale(
            'decision', NEW.decision_type || ':' || NEW.decision_key
        );
    ELSIF OLD.selected_value IS DISTINCT FROM NEW.selected_value
       OR (OLD.status IS DISTINCT FROM NEW.status AND NEW.status <> 'stale')
    THEN
        PERFORM aios.mark_semantic_validation_stale(
            'decision', NEW.decision_type || ':' || NEW.decision_key
        );
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: semantic_validation_neighbor_trigger(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_validation_neighbor_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    pair_forward text;
    pair_reverse text;
BEGIN
    pair_forward := NEW.proposition_id::text || ':' || NEW.neighbor_proposition_id::text;
    pair_reverse := NEW.neighbor_proposition_id::text || ':' || NEW.proposition_id::text;
    PERFORM aios.mark_semantic_validation_stale('semantic_neighbors', NEW.proposition_id::text);
    PERFORM aios.mark_semantic_validation_stale('semantic_neighbors', NEW.neighbor_proposition_id::text);
    PERFORM aios.mark_semantic_validation_stale('semantic_neighbor', pair_forward);
    PERFORM aios.mark_semantic_validation_stale('semantic_neighbor', pair_reverse);
    RETURN NEW;
END;
$$;


--
-- Name: semantic_validation_world_assertion_trigger(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.semantic_validation_world_assertion_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.source_kind='generated_fill'
       AND COALESCE(NEW.meta->>'resolution','') IN (
           'adversarial_corroboration','adversarial_supersession','remain_provisional'
       )
    THEN
        RETURN NEW;
    END IF;

    PERFORM aios.mark_semantic_validation_stale(
        'world_proposition', NEW.world_id::text || ':' || NEW.proposition_id::text
    );
    RETURN NEW;
END;
$$;


--
-- Name: set_semantic_retention_state(text, text, text, text, double precision, double precision, double precision, double precision, jsonb); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.set_semantic_retention_state(p_artifact_type text, p_artifact_id text, p_new_state text, p_reason_code text, p_utility_score double precision DEFAULT NULL::double precision, p_quality_score double precision DEFAULT NULL::double precision, p_redundancy_score double precision DEFAULT NULL::double precision, p_recoverability double precision DEFAULT NULL::double precision, p_meta jsonb DEFAULT '{}'::jsonb) RETURNS boolean
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_old_state text;
    v_protected boolean;
    v_protection_reason text;
    v_scope_key text;
    v_related_node_id uuid;
    v_acquisition_id uuid;
    v_assertion_id uuid;
    v_atom_id uuid;
    v_world_id uuid;
    v_score double precision;
BEGIN
    IF p_new_state NOT IN ('ACTIVE','COLD','QUARANTINED','TRASHED') THEN
        RAISE EXCEPTION 'invalid retention state: %', p_new_state;
    END IF;

    SELECT state INTO v_old_state
    FROM aios.semantic_retention_state
    WHERE artifact_type=p_artifact_type AND artifact_id=p_artifact_id;
    v_old_state := COALESCE(v_old_state, 'ACTIVE');

    SELECT p.protected, p.reason, p.scope_key
    INTO v_protected, v_protection_reason, v_scope_key
    FROM aios.semantic_retention_protection(p_artifact_type,p_artifact_id) p;

    IF COALESCE(v_protected,false) AND p_new_state<>'ACTIVE' THEN
        INSERT INTO aios.semantic_retention_state (
            artifact_type, artifact_id, scope_key, state, reason_code,
            utility_score, quality_score, redundancy_score, recoverability,
            protected, protection_reason, resolver_version, meta,
            first_flagged_at, last_evaluated_at
        )
        VALUES (
            p_artifact_type,p_artifact_id,v_scope_key,v_old_state,
            'protection_blocked:' || p_reason_code,
            p_utility_score,p_quality_score,p_redundancy_score,p_recoverability,
            true,v_protection_reason,'semantic-retention-v1',
            COALESCE(p_meta,'{}'::jsonb) || jsonb_build_object('requested_state',p_new_state),
            now(),now()
        )
        ON CONFLICT (artifact_type,artifact_id) DO UPDATE
        SET scope_key=EXCLUDED.scope_key,
            reason_code=EXCLUDED.reason_code,
            utility_score=EXCLUDED.utility_score,
            quality_score=EXCLUDED.quality_score,
            redundancy_score=EXCLUDED.redundancy_score,
            recoverability=EXCLUDED.recoverability,
            protected=true,
            protection_reason=EXCLUDED.protection_reason,
            meta=aios.semantic_retention_state.meta || EXCLUDED.meta,
            last_evaluated_at=now();

        INSERT INTO aios.retention_event (
            artifact_type,artifact_id,old_state,new_state,reason_code,score,
            triggered_by,policy_version,meta
        ) VALUES (
            p_artifact_type,p_artifact_id,v_old_state,v_old_state,
            'protection_blocked:' || p_reason_code,
            GREATEST(COALESCE(p_redundancy_score,0),1.0-COALESCE(p_quality_score,1.0)),
            'semantic-retention-v1','semantic-retention-v1',
            COALESCE(p_meta,'{}'::jsonb) || jsonb_build_object(
                'requested_state',p_new_state,
                'protection_reason',v_protection_reason
            )
        );
        RETURN false;
    END IF;

    IF v_old_state=p_new_state THEN
        UPDATE aios.semantic_retention_state
        SET reason_code=p_reason_code,
            utility_score=COALESCE(p_utility_score,utility_score),
            quality_score=COALESCE(p_quality_score,quality_score),
            redundancy_score=COALESCE(p_redundancy_score,redundancy_score),
            recoverability=COALESCE(p_recoverability,recoverability),
            protected=COALESCE(v_protected,false),
            protection_reason=v_protection_reason,
            meta=meta || COALESCE(p_meta,'{}'::jsonb),
            last_evaluated_at=now()
        WHERE artifact_type=p_artifact_type AND artifact_id=p_artifact_id;
        RETURN false;
    END IF;

    INSERT INTO aios.semantic_retention_state (
        artifact_type,artifact_id,scope_key,state,reason_code,
        utility_score,quality_score,redundancy_score,recoverability,
        protected,protection_reason,resolver_version,meta,
        first_flagged_at,last_evaluated_at
    ) VALUES (
        p_artifact_type,p_artifact_id,v_scope_key,p_new_state,p_reason_code,
        p_utility_score,p_quality_score,p_redundancy_score,p_recoverability,
        false,NULL,'semantic-retention-v1',COALESCE(p_meta,'{}'::jsonb),now(),now()
    )
    ON CONFLICT (artifact_type,artifact_id) DO UPDATE
    SET scope_key=EXCLUDED.scope_key,
        state=EXCLUDED.state,
        reason_code=EXCLUDED.reason_code,
        utility_score=COALESCE(EXCLUDED.utility_score,aios.semantic_retention_state.utility_score),
        quality_score=COALESCE(EXCLUDED.quality_score,aios.semantic_retention_state.quality_score),
        redundancy_score=COALESCE(EXCLUDED.redundancy_score,aios.semantic_retention_state.redundancy_score),
        recoverability=COALESCE(EXCLUDED.recoverability,aios.semantic_retention_state.recoverability),
        protected=false,
        protection_reason=NULL,
        resolver_version='semantic-retention-v1',
        meta=aios.semantic_retention_state.meta || EXCLUDED.meta,
        last_evaluated_at=now();

    IF p_artifact_type='knowledge_acquisition_event' THEN
        BEGIN
            v_acquisition_id := p_artifact_id::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            v_acquisition_id := NULL;
        END;
        IF v_acquisition_id IS NOT NULL THEN
            SELECT dag_node_id INTO v_related_node_id
            FROM aios.knowledge_acquisition_event
            WHERE acquisition_id=v_acquisition_id;

            IF p_new_state='ACTIVE' THEN
                PERFORM aios.recompute_semantic_evidence_admission(v_acquisition_id);
            ELSE
                UPDATE aios.semantic_evidence_admission
                SET status='suppressed',
                    reason='retention_' || lower(p_new_state),
                    resolver_version='semantic-retention-v1',
                    meta=meta || jsonb_build_object(
                        'retention_state',p_new_state,
                        'retention_reason',p_reason_code
                    ),
                    resolved_at=now(),
                    updated_at=now()
                WHERE acquisition_id=v_acquisition_id;
            END IF;
        END IF;
    ELSIF p_artifact_type='world_proposition_assertion' THEN
        BEGIN
            v_assertion_id := p_artifact_id::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            v_assertion_id := NULL;
        END;
        IF v_assertion_id IS NOT NULL THEN
            SELECT a.world_id,p.atom_id,a.generated_at_node_id
            INTO v_world_id,v_atom_id,v_related_node_id
            FROM aios.world_proposition_assertion a
            JOIN aios.proposition p ON p.proposition_id=a.proposition_id
            WHERE a.assertion_id=v_assertion_id;
            IF v_world_id IS NOT NULL AND v_atom_id IS NOT NULL THEN
                PERFORM aios.reconcile_world_memory_atom(v_world_id,v_atom_id);
            END IF;
        END IF;
    END IF;

    v_score := GREATEST(
        COALESCE(p_redundancy_score,0),
        1.0-COALESCE(p_quality_score,1.0)
    );
    INSERT INTO aios.retention_event (
        artifact_type,artifact_id,old_state,new_state,reason_code,score,
        triggered_by,related_node_id,policy_version,meta
    ) VALUES (
        p_artifact_type,p_artifact_id,v_old_state,p_new_state,p_reason_code,v_score,
        'semantic-retention-v1',v_related_node_id,'semantic-retention-v1',
        COALESCE(p_meta,'{}'::jsonb)
    );

    RETURN true;
END;
$$;


--
-- Name: validate_proposition_conflict_identity(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.validate_proposition_conflict_identity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_atom_a uuid;
    v_atom_b uuid;
    v_predicate_a text;
    v_predicate_b text;
BEGIN
    SELECT atom_id, predicate_norm
    INTO v_atom_a, v_predicate_a
    FROM aios.proposition
    WHERE proposition_id=NEW.proposition_a_id;

    SELECT atom_id, predicate_norm
    INTO v_atom_b, v_predicate_b
    FROM aios.proposition
    WHERE proposition_id=NEW.proposition_b_id;

    IF NEW.conflict_type='opposite_polarity'
       AND v_atom_a IS DISTINCT FROM v_atom_b THEN
        RETURN NULL;
    END IF;

    IF NEW.conflict_type='exclusive_object'
       AND (v_predicate_a='be_definition_of' OR v_predicate_b='be_definition_of') THEN
        RETURN NULL;
    END IF;

    RETURN NEW;
END;
$$;


--
-- Name: validate_world_event_exposure(); Type: FUNCTION; Schema: aios; Owner: -
--

CREATE FUNCTION aios.validate_world_event_exposure() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_world_node_timeline uuid;
    v_character_node_timeline uuid;
    v_link_world_id uuid;
    v_link_world_timeline uuid;
BEGIN
    SELECT timeline_id INTO v_world_node_timeline
      FROM aios.dag_node
     WHERE node_id = NEW.world_node_id;

    IF v_world_node_timeline IS DISTINCT FROM NEW.world_timeline_id THEN
        RAISE EXCEPTION 'world_node_id % is not on world_timeline_id %',
            NEW.world_node_id, NEW.world_timeline_id;
    END IF;

    IF NEW.character_node_id IS NOT NULL THEN
        SELECT timeline_id INTO v_character_node_timeline
          FROM aios.dag_node
         WHERE node_id = NEW.character_node_id;

        IF v_character_node_timeline IS DISTINCT FROM NEW.character_timeline_id THEN
            RAISE EXCEPTION 'character_node_id % is not on character_timeline_id %',
                NEW.character_node_id, NEW.character_timeline_id;
        END IF;
    END IF;

    SELECT world_id, world_timeline_id
      INTO v_link_world_id, v_link_world_timeline
      FROM aios.character_world_timeline_link
     WHERE character_timeline_id = NEW.character_timeline_id;

    IF v_link_world_id IS DISTINCT FROM NEW.world_id
       OR v_link_world_timeline IS DISTINCT FROM NEW.world_timeline_id THEN
        RAISE EXCEPTION 'character timeline % is not linked to world timeline % in world %',
            NEW.character_timeline_id, NEW.world_timeline_id, NEW.world_id;
    END IF;

    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: belief_reconciliation_policy; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.belief_reconciliation_policy (
    policy_key text NOT NULL,
    accept_support double precision NOT NULL,
    decision_margin double precision NOT NULL,
    resolver_version text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT belief_reconciliation_policy_accept_support_check CHECK (((accept_support >= (0)::double precision) AND (accept_support <= (1)::double precision))),
    CONSTRAINT belief_reconciliation_policy_decision_margin_check CHECK (((decision_margin >= (0)::double precision) AND (decision_margin <= (1)::double precision)))
);


--
-- Name: causal_admission; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.causal_admission (
    admission_id uuid DEFAULT gen_random_uuid() NOT NULL,
    candidate_id uuid NOT NULL,
    decision text NOT NULL,
    reason_code text NOT NULL,
    reason_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    state_version_before bigint DEFAULT 0 NOT NULL,
    state_version_after bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT causal_admission_decision_check CHECK ((decision = ANY (ARRAY['ADMITTED'::text, 'ADMITTED_WITH_LATENT_TRANSITION'::text, 'REJECTED_IMPOSSIBLE'::text, 'CONFLICT'::text, 'UNDERDETERMINED'::text, 'FORK_REQUIRED'::text, 'EPISTEMIC_ONLY'::text])))
);


--
-- Name: TABLE causal_admission; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.causal_admission IS 'Causal consistency decision independent of epistemic confidence.';


--
-- Name: causal_candidate; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.causal_candidate (
    candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_id uuid NOT NULL,
    timeline_id uuid NOT NULL,
    dag_node_id uuid,
    domain_id text NOT NULL,
    event_type text NOT NULL,
    entity_id uuid NOT NULL,
    target_entity_id uuid,
    state_key text NOT NULL,
    value_json jsonb,
    parameters jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_kind text DEFAULT 'semantic'::text NOT NULL,
    source_ref text,
    authority_kind text DEFAULT 'semantic_candidate'::text NOT NULL,
    occurred_at timestamp with time zone,
    claim_id uuid,
    frame_id uuid,
    proposition_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: TABLE causal_candidate; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.causal_candidate IS 'Proposed objective state transition; not world truth until causally admitted.';


--
-- Name: causal_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.causal_state (
    world_id uuid NOT NULL,
    timeline_id uuid NOT NULL,
    domain_id text NOT NULL,
    entity_id uuid NOT NULL,
    state_key text NOT NULL,
    value_json jsonb,
    state_version bigint DEFAULT 1 NOT NULL,
    last_event_id uuid,
    last_node_id uuid,
    occurred_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT causal_state_state_version_check CHECK ((state_version >= 1))
);


--
-- Name: TABLE causal_state; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.causal_state IS 'Hidden materialized deterministic state. Never project directly into /char or the HUD.';


--
-- Name: character_belief_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_belief_state (
    instance_id uuid NOT NULL,
    atom_id uuid NOT NULL,
    stance text NOT NULL,
    positive_support double precision DEFAULT 0.0 NOT NULL,
    negative_support double precision DEFAULT 0.0 NOT NULL,
    belief_confidence double precision DEFAULT 0.0 NOT NULL,
    preferred_proposition_id uuid,
    preferred_evidence_instance_id uuid,
    evidence_count integer DEFAULT 0 NOT NULL,
    independent_evidence_count integer DEFAULT 0 NOT NULL,
    resolved_through_node_id uuid,
    resolver_version text DEFAULT 'character-belief-v1'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT character_belief_state_belief_confidence_check CHECK (((belief_confidence >= (0)::double precision) AND (belief_confidence <= (1)::double precision))),
    CONSTRAINT character_belief_state_evidence_count_check CHECK ((evidence_count >= 0)),
    CONSTRAINT character_belief_state_independent_evidence_count_check CHECK ((independent_evidence_count >= 0)),
    CONSTRAINT character_belief_state_negative_support_check CHECK (((negative_support >= (0)::double precision) AND (negative_support <= (1)::double precision))),
    CONSTRAINT character_belief_state_positive_support_check CHECK (((positive_support >= (0)::double precision) AND (positive_support <= (1)::double precision))),
    CONSTRAINT character_belief_state_stance_check CHECK ((stance = ANY (ARRAY['positive'::text, 'negative'::text, 'unresolved'::text])))
);


--
-- Name: TABLE character_belief_state; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.character_belief_state IS 'Convergent current /char belief state. Evidence remains in character_proposition_knowledge and acquisition topology.';


--
-- Name: character_proposition_knowledge; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_proposition_knowledge (
    instance_id uuid NOT NULL,
    proposition_id uuid NOT NULL,
    epistemic_status text DEFAULT 'observed'::text NOT NULL,
    confidence double precision,
    acquisition_mode text NOT NULL,
    source_entity_id uuid,
    first_node_id uuid,
    last_node_id uuid,
    first_acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    base_confidence double precision,
    attention_weight double precision,
    trust_weight double precision,
    compatibility_weight double precision,
    retention_weight double precision,
    salience_weight double precision,
    effective_confidence double precision
);


--
-- Name: claim_context_resolution; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_context_resolution (
    claim_id uuid NOT NULL,
    claim_kind text DEFAULT 'UNKNOWN'::text NOT NULL,
    subject_kind text,
    object_kind text,
    predicate_family text DEFAULT 'UNKNOWN'::text NOT NULL,
    origin_character_id text,
    character_instance_id uuid,
    viewpoint_id text,
    world_id uuid,
    timeline_id uuid,
    dag_node_id uuid,
    epistemic_scope text DEFAULT 'source'::text NOT NULL,
    acquisition_mode text,
    subject_is_pivot boolean DEFAULT false NOT NULL,
    object_is_pivot boolean DEFAULT false NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    resolver_version text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    speaker_id text,
    speaker_type text,
    source_id text,
    source_kind text,
    target_character_id text,
    target_world_id uuid,
    CONSTRAINT claim_context_resolution_confidence_check CHECK (((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision)))
);


--
-- Name: observation; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.observation (
    observation_id uuid DEFAULT gen_random_uuid() NOT NULL,
    claim_id uuid NOT NULL,
    proposition_id uuid NOT NULL,
    document_id uuid,
    timeline_id uuid,
    dag_node_id uuid,
    source_key text,
    source_domain text,
    source_kind text DEFAULT 'observed'::text NOT NULL,
    observed_at timestamp with time zone DEFAULT now() NOT NULL,
    extraction_confidence double precision,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: character_active_proposition_knowledge; Type: VIEW; Schema: aios; Owner: -
--

CREATE VIEW aios.character_active_proposition_knowledge AS
 SELECT bs.instance_id,
    cpk.instance_id AS evidence_instance_id,
    cpk.proposition_id,
        CASE bs.stance
            WHEN 'positive'::text THEN 'believed'::text
            WHEN 'negative'::text THEN 'disbelieved'::text
            ELSE 'uncertain'::text
        END AS epistemic_status,
    bs.belief_confidence AS confidence,
    cpk.acquisition_mode,
    cpk.source_entity_id,
    cpk.first_node_id,
    cpk.last_node_id,
    cpk.first_acquired_at,
    bs.resolved_at AS updated_at,
    (cpk.meta || jsonb_build_object('atom_id', bs.atom_id, 'belief_stance', bs.stance, 'positive_support', bs.positive_support, 'negative_support', bs.negative_support, 'belief_resolver_version', bs.resolver_version)) AS meta,
    cpk.base_confidence,
    cpk.attention_weight,
    cpk.trust_weight,
    cpk.compatibility_weight,
    cpk.retention_weight,
    cpk.salience_weight,
    bs.belief_confidence AS effective_confidence,
    bs.atom_id,
    bs.stance,
    bs.positive_support,
    bs.negative_support,
    bs.evidence_count,
    bs.independent_evidence_count
   FROM (aios.character_belief_state bs
     JOIN aios.character_proposition_knowledge cpk ON (((cpk.instance_id = bs.preferred_evidence_instance_id) AND (cpk.proposition_id = bs.preferred_proposition_id))))
  WHERE ((bs.preferred_proposition_id IS NOT NULL) AND ((bs.stance = 'positive'::text) OR (NOT (EXISTS ( SELECT 1
           FROM (aios.observation obs
             JOIN aios.claim_context_resolution ccr ON ((ccr.claim_id = obs.claim_id)))
          WHERE ((obs.proposition_id = bs.preferred_proposition_id) AND (ccr.claim_kind = 'EVENT'::text)))))));


--
-- Name: VIEW character_active_proposition_knowledge; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON VIEW aios.character_active_proposition_knowledge IS 'Current reconciled /char belief projection. Evidence ownership remains in character_proposition_knowledge. Derived EVENT cognition is agent-visible only when reconciled positive; raw DAG events remain independently visible.';


--
-- Name: character_alias; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_alias (
    alias text NOT NULL,
    character_id text NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: character_epistemic_profile; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_epistemic_profile (
    character_id text NOT NULL,
    skepticism double precision DEFAULT 0.5 NOT NULL,
    curiosity double precision DEFAULT 0.5 NOT NULL,
    authority_trust double precision DEFAULT 0.5 NOT NULL,
    novelty_seeking double precision DEFAULT 0.5 NOT NULL,
    emotional_reactivity double precision DEFAULT 0.5 NOT NULL,
    retention double precision DEFAULT 0.7 NOT NULL,
    source_trust jsonb DEFAULT '{}'::jsonb NOT NULL,
    topic_interest jsonb DEFAULT '{}'::jsonb NOT NULL,
    domain_expertise jsonb DEFAULT '{}'::jsonb NOT NULL,
    trait_weights jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT character_epistemic_profile_authority_trust_check CHECK (((authority_trust >= (0)::double precision) AND (authority_trust <= (1)::double precision))),
    CONSTRAINT character_epistemic_profile_curiosity_check CHECK (((curiosity >= (0)::double precision) AND (curiosity <= (1)::double precision))),
    CONSTRAINT character_epistemic_profile_emotional_reactivity_check CHECK (((emotional_reactivity >= (0)::double precision) AND (emotional_reactivity <= (1)::double precision))),
    CONSTRAINT character_epistemic_profile_novelty_seeking_check CHECK (((novelty_seeking >= (0)::double precision) AND (novelty_seeking <= (1)::double precision))),
    CONSTRAINT character_epistemic_profile_retention_check CHECK (((retention >= (0)::double precision) AND (retention <= (1)::double precision))),
    CONSTRAINT character_epistemic_profile_skepticism_check CHECK (((skepticism >= (0)::double precision) AND (skepticism <= (1)::double precision)))
);


--
-- Name: character_hud_profile; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_hud_profile (
    character_id text NOT NULL,
    profile_id uuid NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: character_hud_readiness; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_hud_readiness (
    instance_id uuid NOT NULL,
    source_timeline_id uuid,
    source_head_node_id uuid,
    source_head_event_id bigint,
    retrieval_ready_node_id uuid,
    retrieval_ready_event_id bigint,
    prepared_source_node_id uuid,
    prepared_source_event_id bigint,
    prepared_state_version bigint,
    status text DEFAULT 'dirty'::text NOT NULL,
    live boolean DEFAULT false NOT NULL,
    dirty_since timestamp with time zone,
    prepared_at timestamp with time zone,
    last_error text,
    hud_json jsonb,
    hud_text text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    cognitive_ready_node_id uuid,
    cognitive_ready_event_id bigint,
    enrichment_ready_node_id uuid,
    enrichment_ready_event_id bigint,
    CONSTRAINT character_hud_readiness_status_check CHECK ((status = ANY (ARRAY['cold'::text, 'dirty'::text, 'preparing'::text, 'ready'::text, 'error'::text])))
);


--
-- Name: character_identity; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_identity (
    character_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    home_world_id uuid,
    process_ontology boolean DEFAULT false NOT NULL,
    canonical_name text,
    display_name text,
    canon text,
    franchise text,
    entity_type text DEFAULT 'character'::text NOT NULL,
    species text,
    gender text,
    age_descriptor text,
    visual_summary text,
    primary_role text,
    archetype text,
    default_tone text[],
    speech_style text,
    content_rating text DEFAULT 'PG'::text,
    moral_constraints text[],
    is_canonical boolean DEFAULT true,
    is_mutable boolean DEFAULT false,
    created_from text,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: character_instance; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_instance (
    instance_id uuid DEFAULT gen_random_uuid() NOT NULL,
    character_id text NOT NULL,
    world_id uuid NOT NULL,
    owner_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    current_world_id uuid,
    parent_instance_id uuid,
    forked_from_node_id uuid
);


--
-- Name: character_inventory; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_inventory (
    inventory_id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    quantity double precision DEFAULT 1 NOT NULL,
    equipped boolean DEFAULT false NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: character_knowledge; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_knowledge (
    instance_id uuid NOT NULL,
    claim_id uuid NOT NULL,
    epistemic_status text DEFAULT 'observed'::text NOT NULL,
    confidence double precision,
    source_entity_id uuid,
    first_node_id uuid,
    last_node_id uuid,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: character_relationship; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_relationship (
    relationship_id uuid DEFAULT gen_random_uuid() NOT NULL,
    observer_instance_id uuid NOT NULL,
    target_entity_id uuid NOT NULL,
    relationship_type text,
    affinity double precision,
    trust double precision,
    familiarity double precision,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: character_runtime_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_runtime_state (
    instance_id uuid NOT NULL,
    world_id uuid NOT NULL,
    timeline_id uuid NOT NULL,
    head_node_id uuid,
    lifecycle_state text DEFAULT 'initializing'::text NOT NULL,
    location_entity_id uuid,
    health double precision,
    stamina double precision,
    energy double precision,
    physical_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    emotional_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    social_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    goals jsonb DEFAULT '[]'::jsonb NOT NULL,
    active_tasks jsonb DEFAULT '[]'::jsonb NOT NULL,
    runtime_flags jsonb DEFAULT '{}'::jsonb NOT NULL,
    state_version bigint DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source_timeline_id uuid,
    source_head_node_id uuid
);


--
-- Name: COLUMN character_runtime_state.location_entity_id; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON COLUMN aios.character_runtime_state.location_entity_id IS 'Legacy compatibility field. Canonical location is causal_state world.location and its /world located_in projection.';


--
-- Name: COLUMN character_runtime_state.health; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON COLUMN aios.character_runtime_state.health IS 'Legacy compatibility field; deterministic health belongs in a causal domain, not HUD runtime state.';


--
-- Name: COLUMN character_runtime_state.stamina; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON COLUMN aios.character_runtime_state.stamina IS 'Legacy compatibility field; deterministic stamina belongs in a causal domain, not HUD runtime state.';


--
-- Name: COLUMN character_runtime_state.energy; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON COLUMN aios.character_runtime_state.energy IS 'Legacy compatibility field; deterministic energy belongs in a causal domain, not HUD runtime state.';


--
-- Name: character_world_timeline_link; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.character_world_timeline_link (
    character_timeline_id uuid NOT NULL,
    world_id uuid NOT NULL,
    world_timeline_id uuid NOT NULL,
    relation text DEFAULT 'inhabits'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT character_world_timeline_link_check CHECK ((character_timeline_id <> world_timeline_id)),
    CONSTRAINT character_world_timeline_link_relation_check CHECK ((relation = ANY (ARRAY['inhabits'::text, 'observes'::text, 'projects_from'::text])))
);


--
-- Name: claim_candidate; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_candidate (
    claim_id uuid DEFAULT gen_random_uuid() NOT NULL,
    sentence_id uuid NOT NULL,
    subject text,
    predicate text,
    object text,
    raw_text text NOT NULL,
    confidence real DEFAULT 0.0 NOT NULL,
    extraction_rule text,
    extraction_ver text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    subject_normalized text,
    claim_bucket text,
    pruning_reason text
);


--
-- Name: claim_candidate_full_spo; Type: VIEW; Schema: aios; Owner: -
--

CREATE VIEW aios.claim_candidate_full_spo AS
 SELECT claim_id,
    sentence_id,
    subject,
    predicate,
    object,
    raw_text,
    confidence,
    extraction_rule,
    extraction_ver,
    status,
    created_at
   FROM aios.claim_candidate
  WHERE ((subject IS NOT NULL) AND (predicate IS NOT NULL) AND (object IS NOT NULL));


--
-- Name: claim_contradiction_candidate; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_contradiction_candidate (
    contradiction_id uuid DEFAULT gen_random_uuid() NOT NULL,
    claim_a_id uuid NOT NULL,
    claim_b_id uuid NOT NULL,
    similarity real NOT NULL,
    contradiction_score real NOT NULL,
    reasons jsonb NOT NULL,
    detector_ver text NOT NULL,
    detector_conf jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT claim_contradiction_candidate_check CHECK ((claim_a_id < claim_b_id))
);


--
-- Name: claim_provenance; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_provenance (
    claim_id uuid NOT NULL,
    document_id uuid NOT NULL,
    citation text,
    source_weight real DEFAULT 0.5 NOT NULL
);


--
-- Name: claim_quality; Type: VIEW; Schema: aios; Owner: -
--

CREATE VIEW aios.claim_quality AS
 SELECT claim_id,
    subject,
    predicate,
    object,
    raw_text,
    ((((subject IS NOT NULL))::integer + ((predicate IS NOT NULL))::integer) + ((object IS NOT NULL))::integer) AS structural_score
   FROM aios.claim_candidate;


--
-- Name: claim_semantic_frame; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_semantic_frame (
    frame_id uuid DEFAULT gen_random_uuid() NOT NULL,
    claim_id uuid NOT NULL,
    frame_index integer NOT NULL,
    parent_frame_id uuid,
    object_frame_id uuid,
    subject_text text,
    predicate_surface text,
    predicate_canonical text,
    object_text text,
    resolved_subject text,
    resolved_object text,
    subject_entity_key text,
    object_entity_key text,
    subject_kind_guess text,
    object_kind_guess text,
    polarity smallint DEFAULT 1 NOT NULL,
    modality text DEFAULT 'asserted'::text NOT NULL,
    tense text,
    aspect text,
    discourse_mode text DEFAULT 'narrated_observation'::text NOT NULL,
    frame_role text DEFAULT 'clause'::text NOT NULL,
    resolution_status text DEFAULT 'unresolved'::text NOT NULL,
    extraction_confidence double precision DEFAULT 0.0 NOT NULL,
    predicate_confidence double precision DEFAULT 0.0 NOT NULL,
    entity_confidence double precision DEFAULT 0.0 NOT NULL,
    referent_confidence double precision DEFAULT 0.0 NOT NULL,
    frame_confidence double precision DEFAULT 0.0 NOT NULL,
    canonical_text text,
    decomposer_version text NOT NULL,
    resolver_version text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    CONSTRAINT claim_semantic_frame_entity_confidence_check CHECK (((entity_confidence >= (0.0)::double precision) AND (entity_confidence <= (1.0)::double precision))),
    CONSTRAINT claim_semantic_frame_extraction_confidence_check CHECK (((extraction_confidence >= (0.0)::double precision) AND (extraction_confidence <= (1.0)::double precision))),
    CONSTRAINT claim_semantic_frame_frame_confidence_check CHECK (((frame_confidence >= (0.0)::double precision) AND (frame_confidence <= (1.0)::double precision))),
    CONSTRAINT claim_semantic_frame_polarity_check CHECK ((polarity = ANY (ARRAY['-1'::integer, 1]))),
    CONSTRAINT claim_semantic_frame_predicate_confidence_check CHECK (((predicate_confidence >= (0.0)::double precision) AND (predicate_confidence <= (1.0)::double precision))),
    CONSTRAINT claim_semantic_frame_referent_confidence_check CHECK (((referent_confidence >= (0.0)::double precision) AND (referent_confidence <= (1.0)::double precision))),
    CONSTRAINT claim_semantic_frame_resolution_status_check CHECK ((resolution_status = ANY (ARRAY['unresolved'::text, 'partial'::text, 'resolved'::text, 'ambiguous'::text])))
);


--
-- Name: claim_semantic_frame_projection; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_semantic_frame_projection (
    claim_id uuid NOT NULL,
    primary_frame_id uuid,
    decomposer_version text NOT NULL,
    resolver_version text,
    frame_count integer DEFAULT 0 NOT NULL,
    resolved_count integer DEFAULT 0 NOT NULL,
    projected_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: claim_similarity_edge; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_similarity_edge (
    claim_a_id uuid NOT NULL,
    claim_b_id uuid NOT NULL,
    similarity real NOT NULL,
    embedding_model text NOT NULL,
    embedding_version text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT claim_similarity_edge_check CHECK ((claim_a_id < claim_b_id))
);


--
-- Name: claim_world_assignment; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_world_assignment (
    claim_id uuid NOT NULL,
    world_key text NOT NULL,
    confidence real DEFAULT 0.5 NOT NULL,
    assigned_by text NOT NULL,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: claims_normalized; Type: VIEW; Schema: aios; Owner: -
--

CREATE VIEW aios.claims_normalized AS
 SELECT claim_id,
    lower(TRIM(BOTH FROM subject)) AS norm_subject,
    lower(TRIM(BOTH FROM predicate)) AS norm_predicate,
    lower(TRIM(BOTH FROM object)) AS norm_object,
    raw_text,
    sentence_id
   FROM aios.claim_candidate cc;


--
-- Name: dag_edge; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.dag_edge (
    timeline_id uuid NOT NULL,
    parent_node_id uuid NOT NULL,
    child_node_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    edge_type text DEFAULT 'next'::text NOT NULL,
    CONSTRAINT dag_edge_check CHECK ((parent_node_id <> child_node_id))
);


--
-- Name: dag_node; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.dag_node (
    node_id uuid DEFAULT gen_random_uuid() NOT NULL,
    timeline_id uuid NOT NULL,
    event_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    character_id text,
    kind aios.event_kind DEFAULT 'other'::aios.event_kind NOT NULL,
    speaker_id text,
    speaker_role aios.actor_type,
    recipient_id text,
    message_text text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    origin aios.node_origin DEFAULT 'agent_action'::aios.node_origin NOT NULL,
    event_time timestamp with time zone,
    viewpoint_id text
);


--
-- Name: document_metadata_observation; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.document_metadata_observation (
    metadata_id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    field_type text NOT NULL,
    raw_value text NOT NULL,
    normalized_value text,
    source_unit_id uuid,
    source_location text,
    confidence double precision DEFAULT 0.5 NOT NULL,
    extraction_method text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: document_section; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.document_section (
    section_id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid,
    section_path text NOT NULL,
    section_order integer NOT NULL,
    content text NOT NULL,
    node_id uuid,
    claims_extracted_at timestamp with time zone
);


--
-- Name: document_unit; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.document_unit (
    unit_id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    parent_unit_id uuid,
    node_id uuid,
    unit_type text NOT NULL,
    unit_index integer NOT NULL,
    path text NOT NULL,
    title text,
    content text,
    start_char integer,
    end_char integer,
    depth integer DEFAULT 0 NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: entity_controller; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.entity_controller (
    controller_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    controller_type text NOT NULL,
    controller_ref text NOT NULL,
    authority text DEFAULT 'primary'::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: extracted_sentence; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.extracted_sentence (
    sentence_id uuid DEFAULT gen_random_uuid() NOT NULL,
    section_id uuid NOT NULL,
    sentence_index integer NOT NULL,
    sentence_text text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hud_profile; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.hud_profile (
    profile_id uuid DEFAULT gen_random_uuid() NOT NULL,
    profile_name text NOT NULL,
    description text,
    token_budget integer DEFAULT 1600 NOT NULL,
    recent_event_limit integer DEFAULT 12 NOT NULL,
    memory_budget integer DEFAULT 350 NOT NULL,
    belief_budget integer DEFAULT 320 NOT NULL,
    relationship_budget integer DEFAULT 160 NOT NULL,
    scene_budget integer DEFAULT 260 NOT NULL,
    inventory_budget integer DEFAULT 140 NOT NULL,
    rules_budget integer DEFAULT 140 NOT NULL,
    goals_budget integer DEFAULT 140 NOT NULL,
    entity_hops integer DEFAULT 1 NOT NULL,
    semantic_retrieval_limit integer DEFAULT 25 NOT NULL,
    deep_memory_limit integer DEFAULT 0 NOT NULL,
    include_emotional_state boolean DEFAULT true NOT NULL,
    include_physical_state boolean DEFAULT true NOT NULL,
    include_social_state boolean DEFAULT true NOT NULL,
    include_inventory boolean DEFAULT true NOT NULL,
    include_relationships boolean DEFAULT true NOT NULL,
    include_conflicts boolean DEFAULT true NOT NULL,
    include_provenance boolean DEFAULT true NOT NULL,
    include_confidence boolean DEFAULT true NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hud_profile_belief_budget_check CHECK ((belief_budget >= 0)),
    CONSTRAINT hud_profile_deep_memory_limit_check CHECK ((deep_memory_limit >= 0)),
    CONSTRAINT hud_profile_entity_hops_check CHECK (((entity_hops >= 0) AND (entity_hops <= 4))),
    CONSTRAINT hud_profile_goals_budget_check CHECK ((goals_budget >= 0)),
    CONSTRAINT hud_profile_inventory_budget_check CHECK ((inventory_budget >= 0)),
    CONSTRAINT hud_profile_memory_budget_check CHECK ((memory_budget >= 0)),
    CONSTRAINT hud_profile_recent_event_limit_check CHECK ((recent_event_limit >= 0)),
    CONSTRAINT hud_profile_relationship_budget_check CHECK ((relationship_budget >= 0)),
    CONSTRAINT hud_profile_rules_budget_check CHECK ((rules_budget >= 0)),
    CONSTRAINT hud_profile_scene_budget_check CHECK ((scene_budget >= 0)),
    CONSTRAINT hud_profile_semantic_retrieval_limit_check CHECK ((semantic_retrieval_limit >= 0)),
    CONSTRAINT hud_profile_token_budget_check CHECK ((token_budget > 0))
);


--
-- Name: ingest_event; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.ingest_event (
    event_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    event_time timestamp with time zone,
    source text,
    source_event_id text,
    kind aios.event_kind DEFAULT 'other'::aios.event_kind NOT NULL,
    session_id uuid,
    speaker_id text,
    speaker_role aios.actor_type,
    recipient_id text,
    character_id text,
    user_name text,
    message_text text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    dedupe_key text NOT NULL,
    processed_at timestamp with time zone,
    process_status aios.process_status DEFAULT 'new'::aios.process_status NOT NULL,
    process_error text,
    dag_processed_at timestamp with time zone,
    section_processed_at timestamp with time zone,
    claims_processed_at timestamp with time zone,
    rdf_processed_at timestamp with time zone,
    rdf_error text,
    viewpoint_id text,
    source_id text,
    source_kind text,
    target_character_id text,
    target_world_id uuid,
    provenance_version text DEFAULT 'provenance-v1'::text NOT NULL,
    superseded_at timestamp with time zone,
    superseded_by_event_id bigint
);


--
-- Name: ingest_event_event_id_seq; Type: SEQUENCE; Schema: aios; Owner: -
--

CREATE SEQUENCE aios.ingest_event_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ingest_event_event_id_seq; Type: SEQUENCE OWNED BY; Schema: aios; Owner: -
--

ALTER SEQUENCE aios.ingest_event_event_id_seq OWNED BY aios.ingest_event.event_id;


--
-- Name: knowledge_acquisition_event; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.knowledge_acquisition_event (
    acquisition_id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_id uuid NOT NULL,
    proposition_id uuid,
    claim_id uuid,
    acquisition_mode text NOT NULL,
    epistemic_status text DEFAULT 'observed'::text NOT NULL,
    confidence double precision,
    source_entity_id uuid,
    dag_node_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT knowledge_acquisition_event_check CHECK (((proposition_id IS NOT NULL) OR (claim_id IS NOT NULL)))
);


--
-- Name: memory_item; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.memory_item (
    memory_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    timeline_id uuid NOT NULL,
    thread_root_node_id uuid,
    derived_from_node_id uuid,
    derived_from_event_id bigint,
    kind text DEFAULT 'fact'::text NOT NULL,
    content text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: memory_item_memory_id_seq; Type: SEQUENCE; Schema: aios; Owner: -
--

CREATE SEQUENCE aios.memory_item_memory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: memory_item_memory_id_seq; Type: SEQUENCE OWNED BY; Schema: aios; Owner: -
--

ALTER SEQUENCE aios.memory_item_memory_id_seq OWNED BY aios.memory_item.memory_id;


--
-- Name: memory_reconciliation_receipt; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.memory_reconciliation_receipt (
    receipt_id uuid DEFAULT gen_random_uuid() NOT NULL,
    claim_id uuid,
    assertion_id uuid,
    proposition_id uuid NOT NULL,
    atom_id uuid NOT NULL,
    path text NOT NULL,
    scope_key text NOT NULL,
    outcome text NOT NULL,
    resolver_version text DEFAULT 'memory-reconciliation-v1'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    reconciled_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_reconciliation_receipt_check CHECK ((num_nonnulls(claim_id, assertion_id) = 1)),
    CONSTRAINT memory_reconciliation_receipt_path_check CHECK ((path = ANY (ARRAY['char'::text, 'world'::text, 'evidence'::text])))
);


--
-- Name: TABLE memory_reconciliation_receipt; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.memory_reconciliation_receipt IS 'Audit boundary for current-memory routing. Normalized claims route to /char or evidence-only; explicit world assertions route independently to /world.';


--
-- Name: message_cognitive_commit; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.message_cognitive_commit (
    commit_id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_id uuid NOT NULL,
    node_id uuid NOT NULL,
    timeline_id uuid,
    event_id bigint,
    character_id text NOT NULL,
    speaker_id text,
    speaker_role text,
    viewpoint_id text,
    interpreter_version text NOT NULL,
    source_text_hash text NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    committed_at timestamp with time zone DEFAULT now() NOT NULL,
    enrichment_completed_at timestamp with time zone
);


--
-- Name: message_cognitive_unit; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.message_cognitive_unit (
    unit_id uuid DEFAULT gen_random_uuid() NOT NULL,
    commit_id uuid NOT NULL,
    ordinal integer NOT NULL,
    claim_kind text NOT NULL,
    text text NOT NULL,
    topic_key text NOT NULL,
    polarity smallint DEFAULT 1 NOT NULL,
    salience double precision DEFAULT 0.5 NOT NULL,
    confidence double precision DEFAULT 0.5 NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    supersedes_unit_id uuid,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: narrative_cluster; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.narrative_cluster (
    narrative_id uuid DEFAULT gen_random_uuid() NOT NULL,
    topic_key text NOT NULL,
    narrative_key text NOT NULL,
    label text,
    summary text,
    confidence double precision DEFAULT 0.5 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: narrative_membership; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.narrative_membership (
    narrative_id uuid NOT NULL,
    observation_id uuid NOT NULL,
    affinity double precision DEFAULT 1.0 NOT NULL,
    assigned_by text DEFAULT 'deterministic-v1'::text NOT NULL,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: narrative_source_affinity; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.narrative_source_affinity (
    narrative_id uuid NOT NULL,
    source_key text NOT NULL,
    observation_count integer DEFAULT 0 NOT NULL,
    affinity double precision DEFAULT 0.0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: observation_proposition; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.observation_proposition (
    observation_id uuid NOT NULL,
    proposition_id uuid NOT NULL,
    frame_id uuid NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    semantic_role text DEFAULT 'derived_frame'::text NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT observation_proposition_confidence_check CHECK (((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision)))
);


--
-- Name: pipeline_job; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.pipeline_job (
    job_id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_type text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    run_after timestamp with time zone DEFAULT now() NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    resource_class text DEFAULT 'GLOBAL'::text NOT NULL,
    partition_key text,
    worker_id text,
    claimed_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    lease_expires_at timestamp with time zone,
    scheduling_lane text DEFAULT 'DEFAULT'::text NOT NULL,
    CONSTRAINT ck_pipeline_job_resource_class CHECK ((resource_class = ANY (ARRAY['FAST_SQL'::text, 'NLP'::text, 'SEMANTIC'::text, 'VECTOR'::text, 'RDF'::text, 'RECONCILIATION'::text, 'GLOBAL'::text]))),
    CONSTRAINT ck_pipeline_job_scheduling_lane CHECK ((scheduling_lane = ANY (ARRAY['LIVE'::text, 'STRUCTURAL'::text, 'BACKGROUND'::text, 'DEFAULT'::text])))
);


--
-- Name: pipeline_stage_config; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.pipeline_stage_config (
    stage_name text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    max_batch integer,
    world_key text,
    character_id text
);


--
-- Name: proposition; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.proposition (
    proposition_id uuid DEFAULT gen_random_uuid() NOT NULL,
    proposition_hash text NOT NULL,
    topic_key text NOT NULL,
    subject_norm text,
    predicate_norm text,
    object_norm text,
    polarity smallint DEFAULT 1 NOT NULL,
    canonical_text text NOT NULL,
    modality text DEFAULT 'asserted'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    atom_id uuid NOT NULL,
    CONSTRAINT proposition_polarity_check CHECK ((polarity = ANY (ARRAY['-1'::integer, 1])))
);


--
-- Name: proposition_conflict; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.proposition_conflict (
    conflict_id uuid DEFAULT gen_random_uuid() NOT NULL,
    topic_key text NOT NULL,
    proposition_a_id uuid NOT NULL,
    proposition_b_id uuid NOT NULL,
    conflict_type text NOT NULL,
    strength double precision DEFAULT 1.0 NOT NULL,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT proposition_conflict_check CHECK ((proposition_a_id <> proposition_b_id))
);


--
-- Name: proposition_evidence; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.proposition_evidence (
    evidence_id uuid DEFAULT gen_random_uuid() NOT NULL,
    proposition_id uuid NOT NULL,
    observation_id uuid,
    evidence_role text DEFAULT 'support'::text NOT NULL,
    source_weight double precision DEFAULT 0.5 NOT NULL,
    confidence double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: rdf_promotion_log; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.rdf_promotion_log (
    promotion_id uuid DEFAULT gen_random_uuid() NOT NULL,
    claim_id uuid NOT NULL,
    rdf_dataset text NOT NULL,
    rdf_graph text NOT NULL,
    rdf_subject text NOT NULL,
    rdf_predicate text NOT NULL,
    rdf_object text NOT NULL,
    promoted_at timestamp with time zone DEFAULT now() NOT NULL,
    promoted_by text NOT NULL,
    promotion_meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: reconciliation_family_policy; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.reconciliation_family_policy (
    predicate_family text NOT NULL,
    policy_name text NOT NULL,
    policy_mode text NOT NULL,
    accept_support double precision NOT NULL,
    decision_margin double precision NOT NULL,
    exclusive_slot boolean DEFAULT false NOT NULL,
    resolver_version text DEFAULT 'semantic-policy-v1'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT reconciliation_family_policy_accept_support_check CHECK (((accept_support >= (0)::double precision) AND (accept_support <= (1)::double precision))),
    CONSTRAINT reconciliation_family_policy_decision_margin_check CHECK (((decision_margin >= (0)::double precision) AND (decision_margin <= (1)::double precision))),
    CONSTRAINT reconciliation_family_policy_policy_mode_check CHECK ((policy_mode = ANY (ARRAY['accumulate'::text, 'latest'::text, 'max'::text])))
);


--
-- Name: retention_event; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.retention_event (
    event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    artifact_type text NOT NULL,
    artifact_id text NOT NULL,
    old_state text NOT NULL,
    new_state text NOT NULL,
    reason_code text NOT NULL,
    score double precision,
    triggered_by text DEFAULT 'semantic-retention-v1'::text NOT NULL,
    related_node_id uuid,
    policy_version text DEFAULT 'semantic-retention-v1'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT retention_event_new_state_check CHECK ((new_state = ANY (ARRAY['ACTIVE'::text, 'COLD'::text, 'QUARANTINED'::text, 'TRASHED'::text]))),
    CONSTRAINT retention_event_old_state_check CHECK ((old_state = ANY (ARRAY['ACTIVE'::text, 'COLD'::text, 'QUARANTINED'::text, 'TRASHED'::text]))),
    CONSTRAINT retention_event_score_check CHECK (((score IS NULL) OR ((score >= (0)::double precision) AND (score <= (1)::double precision))))
);


--
-- Name: TABLE retention_event; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.retention_event IS 'Append-only audit history for logical retention transitions.';


--
-- Name: semantic_anchor_edge; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_anchor_edge (
    anchor_edge_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_scope_key text NOT NULL,
    source_node_id uuid NOT NULL,
    target_scope_key text NOT NULL,
    target_node_id uuid NOT NULL,
    relationship_type text NOT NULL,
    character_id text,
    character_instance_id uuid,
    world_id uuid,
    proposition_id uuid,
    acquisition_id uuid,
    dag_node_id uuid,
    confidence double precision DEFAULT 1.0 NOT NULL,
    inference_source text DEFAULT 'deterministic'::text NOT NULL,
    inference_status text DEFAULT 'accepted'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_anchor_edge_check CHECK ((source_node_id <> target_node_id)),
    CONSTRAINT semantic_anchor_edge_check1 CHECK ((source_scope_key <> target_scope_key)),
    CONSTRAINT semantic_anchor_edge_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))
);


--
-- Name: semantic_atom; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_atom (
    atom_id uuid DEFAULT gen_random_uuid() NOT NULL,
    atom_key text NOT NULL,
    subject_norm text,
    predicate_norm text,
    object_norm text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE semantic_atom; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_atom IS 'Polarity-independent semantic question identity. Proposition rows remain evidence assertions about an atom.';


--
-- Name: semantic_boundary_classification; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_boundary_classification (
    classification_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    cluster_a_id uuid NOT NULL,
    cluster_b_id uuid NOT NULL,
    classification text NOT NULL,
    confidence double precision NOT NULL,
    classifier_version text NOT NULL,
    status text DEFAULT 'candidate'::text NOT NULL,
    feature_scores jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_boundary_classification_classification_check CHECK ((classification = ANY (ARRAY['SAME_REGION'::text, 'TOPIC_SPLIT'::text, 'TEMPORAL_TRANSITION'::text, 'STATE_TRANSITION'::text, 'NARRATIVE_SPLIT'::text, 'CONTRADICTION_CLUSTER'::text, 'EXPERIENTIAL_BRANCH_CANDIDATE'::text, 'WORLD_BRANCH_CANDIDATE'::text, 'UNRESOLVED'::text]))),
    CONSTRAINT semantic_boundary_classification_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))
);


--
-- Name: TABLE semantic_boundary_classification; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_boundary_classification IS 'Advisory interpretation of cluster boundaries. Branch candidate labels never create branches automatically.';


--
-- Name: semantic_branch_candidate; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_branch_candidate (
    branch_candidate_id uuid DEFAULT gen_random_uuid() NOT NULL,
    boundary_classification_id uuid NOT NULL,
    run_id uuid NOT NULL,
    scope_key text NOT NULL,
    scope_partition_key text NOT NULL,
    scope_kind text NOT NULL,
    candidate_kind text NOT NULL,
    cluster_a_id uuid NOT NULL,
    cluster_b_id uuid NOT NULL,
    character_id text,
    character_instance_id uuid,
    world_id uuid,
    timeline_id uuid,
    confidence double precision NOT NULL,
    status text DEFAULT 'candidate'::text NOT NULL,
    reason jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_branch_candidate_candidate_kind_check CHECK ((candidate_kind = ANY (ARRAY['experiential'::text, 'world'::text]))),
    CONSTRAINT semantic_branch_candidate_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))
);


--
-- Name: TABLE semantic_branch_candidate; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_branch_candidate IS 'Classifier-derived branch proposals only. Runtime/world creation requires a separate authoritative promotion decision.';


--
-- Name: semantic_cluster_boundary; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_cluster_boundary (
    run_id uuid NOT NULL,
    cluster_a_id uuid NOT NULL,
    cluster_b_id uuid NOT NULL,
    edge_count integer NOT NULL,
    mean_similarity double precision NOT NULL,
    max_similarity double precision NOT NULL,
    min_similarity double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT semantic_cluster_boundary_check CHECK ((cluster_a_id <> cluster_b_id))
);


--
-- Name: TABLE semantic_cluster_boundary; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_cluster_boundary IS 'Cross-cluster vector bridges retained for later topic/state/narrative/branch separation analysis.';


--
-- Name: semantic_cluster_candidate; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_cluster_candidate (
    cluster_id uuid DEFAULT gen_random_uuid() NOT NULL,
    cluster_key uuid NOT NULL,
    run_id uuid NOT NULL,
    embedding_version text NOT NULL,
    algorithm_version text NOT NULL,
    member_count integer NOT NULL,
    internal_edge_count integer DEFAULT 0 NOT NULL,
    density double precision DEFAULT 0.0 NOT NULL,
    cohesion double precision DEFAULT 0.0 NOT NULL,
    boundary_strength double precision DEFAULT 0.0 NOT NULL,
    separation double precision DEFAULT 0.0 NOT NULL,
    status text DEFAULT 'candidate'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: TABLE semantic_cluster_candidate; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_cluster_candidate IS 'Advisory vector-geometry clusters. Classification into topic, state transition, narrative split, or branch requires later semantic/context validation.';


--
-- Name: semantic_cluster_classification; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_cluster_classification (
    classification_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    classification text NOT NULL,
    confidence double precision NOT NULL,
    classifier_version text NOT NULL,
    status text DEFAULT 'candidate'::text NOT NULL,
    feature_scores jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_cluster_classification_classification_check CHECK ((classification = ANY (ARRAY['TOPIC_REGION'::text, 'STATE_SERIES'::text, 'EVENT_REGION'::text, 'MEMORY_REGION'::text, 'BELIEF_REGION'::text, 'RULE_REGION'::text, 'GOAL_REGION'::text, 'MIXED_REGION'::text, 'UNRESOLVED'::text]))),
    CONSTRAINT semantic_cluster_classification_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))
);


--
-- Name: TABLE semantic_cluster_classification; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_cluster_classification IS 'Advisory interpretation of semantic cluster contents for later topology routing and pruning.';


--
-- Name: semantic_cluster_membership; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_cluster_membership (
    cluster_id uuid NOT NULL,
    proposition_id uuid NOT NULL,
    membership_kind text NOT NULL,
    affinity double precision DEFAULT 0.0 NOT NULL,
    internal_degree integer DEFAULT 0 NOT NULL,
    strongest_neighbor_id uuid,
    strongest_similarity double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_cluster_membership_membership_kind_check CHECK ((membership_kind = ANY (ARRAY['core'::text, 'fringe'::text])))
);


--
-- Name: semantic_cluster_run; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_cluster_run (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    embedding_version text NOT NULL,
    algorithm_version text NOT NULL,
    core_threshold double precision NOT NULL,
    attach_threshold double precision NOT NULL,
    min_cluster_size integer NOT NULL,
    structure_watermark timestamp with time zone,
    config_signature text NOT NULL,
    cluster_count integer DEFAULT 0 NOT NULL,
    outlier_count integer DEFAULT 0 NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    status text DEFAULT 'running'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: semantic_evidence_admission; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_evidence_admission (
    acquisition_id uuid NOT NULL,
    status text NOT NULL,
    reason text NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    resolver_version text DEFAULT 'semantic-admission-v1'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_evidence_admission_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT semantic_evidence_admission_status_check CHECK ((status = ANY (ARRAY['active'::text, 'unresolved'::text, 'suppressed'::text])))
);


--
-- Name: TABLE semantic_evidence_admission; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_evidence_admission IS 'Admission decision for character cognition. Raw evidence is never deleted; unresolved/suppressed evidence remains available for diagnostics and later reinterpretation.';


--
-- Name: semantic_interpretation; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_interpretation (
    interpretation_id uuid DEFAULT gen_random_uuid() NOT NULL,
    claim_id uuid NOT NULL,
    frame_id uuid NOT NULL,
    semantic_type text NOT NULL,
    source_language text DEFAULT 'en'::text NOT NULL,
    standalone_semantic boolean DEFAULT false NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    interpreter_version text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    interpreted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_interpretation_confidence_check CHECK (((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision))),
    CONSTRAINT semantic_interpretation_type_check CHECK ((semantic_type = ANY (ARRAY['ACTION'::text, 'CAUSE'::text, 'COMMUNICATION'::text, 'DESIRE'::text, 'DESCRIPTION'::text, 'IDENTITY'::text, 'INTENTION'::text, 'LOCATION'::text, 'MEMORY'::text, 'MENTAL_STATE'::text, 'POSSESSION'::text, 'RELATION'::text, 'RULE'::text, 'TEMPORAL'::text, 'UNKNOWN'::text])))
);


--
-- Name: TABLE semantic_interpretation; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_interpretation IS 'Language-neutral semantic meaning derived from a source-language claim frame; evidence is preserved separately in claim_semantic_frame.';


--
-- Name: COLUMN semantic_interpretation.standalone_semantic; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON COLUMN aios.semantic_interpretation.standalone_semantic IS 'Whether this interpretation is safe to promote as an independent atomic proposition rather than only nested semantic content.';


--
-- Name: semantic_interpretation_role; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_interpretation_role (
    interpretation_id uuid NOT NULL,
    role_name text NOT NULL,
    ordinal integer DEFAULT 0 NOT NULL,
    value_text text,
    entity_key text,
    child_frame_id uuid,
    confidence double precision DEFAULT 1.0 NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT semantic_interpretation_role_confidence_check CHECK (((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision))),
    CONSTRAINT semantic_interpretation_role_target_check CHECK (((value_text IS NOT NULL) OR (entity_key IS NOT NULL) OR (child_frame_id IS NOT NULL)))
);


--
-- Name: TABLE semantic_interpretation_role; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_interpretation_role IS 'Typed arguments/content links for a semantic interpretation; child_frame_id expresses nested semantic content without frame:N text leakage.';


--
-- Name: semantic_neighbor_candidate; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_neighbor_candidate (
    proposition_id uuid NOT NULL,
    neighbor_proposition_id uuid NOT NULL,
    similarity double precision NOT NULL,
    relation_hint text DEFAULT 'semantic_neighbor'::text NOT NULL,
    status text DEFAULT 'candidate'::text NOT NULL,
    embedding_version text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_neighbor_candidate_check CHECK ((proposition_id <> neighbor_proposition_id))
);


--
-- Name: TABLE semantic_neighbor_candidate; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_neighbor_candidate IS 'Advisory vector-neighbor candidates only. Never authoritative for truth, world membership, branch membership, equivalence, or epistemic visibility.';


--
-- Name: semantic_neighbor_relation; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_neighbor_relation (
    proposition_id uuid NOT NULL,
    neighbor_proposition_id uuid NOT NULL,
    embedding_version text NOT NULL,
    relation text NOT NULL,
    confidence double precision NOT NULL,
    classifier_version text NOT NULL,
    status text DEFAULT 'candidate'::text NOT NULL,
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_neighbor_relation_check CHECK ((proposition_id <> neighbor_proposition_id)),
    CONSTRAINT semantic_neighbor_relation_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT semantic_neighbor_relation_relation_check CHECK ((relation = ANY (ARRAY['EQUIVALENT'::text, 'REFINES'::text, 'CONTRADICTS'::text, 'SAME_TOPIC'::text, 'SAME_EVENT'::text, 'RELATED'::text, 'UNRESOLVED'::text])))
);


--
-- Name: TABLE semantic_neighbor_relation; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_neighbor_relation IS 'Advisory pairwise semantic interpretation of vector-neighbor propositions.';


--
-- Name: semantic_outlier_candidate; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_outlier_candidate (
    run_id uuid NOT NULL,
    proposition_id uuid NOT NULL,
    embedding_version text NOT NULL,
    reason text NOT NULL,
    nearest_similarity double precision,
    nearest_proposition_id uuid,
    status text DEFAULT 'candidate'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE semantic_outlier_candidate; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_outlier_candidate IS 'Advisory semantic outliers. Never delete, reject, or demote propositions solely from this table.';


--
-- Name: semantic_reconciliation_receipt; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_reconciliation_receipt (
    receipt_id uuid DEFAULT gen_random_uuid() NOT NULL,
    receipt_key text NOT NULL,
    source_kind text NOT NULL,
    source_id text NOT NULL,
    scope_key text NOT NULL,
    scope_partition_key text NOT NULL,
    action text NOT NULL,
    topology_node_id uuid,
    topology_edge_id uuid,
    rdf_dataset text,
    rdf_graph text,
    classifier_version text,
    confidence double precision,
    status text DEFAULT 'accepted'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    reconciled_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_reconciliation_receipt_confidence_check CHECK (((confidence IS NULL) OR ((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))),
    CONSTRAINT semantic_reconciliation_receipt_source_kind_check CHECK ((source_kind = ANY (ARRAY['neighbor_relation'::text, 'cluster'::text, 'boundary'::text])))
);


--
-- Name: semantic_retention_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_retention_state (
    artifact_type text NOT NULL,
    artifact_id text NOT NULL,
    scope_key text,
    state text NOT NULL,
    reason_code text NOT NULL,
    utility_score double precision,
    quality_score double precision,
    redundancy_score double precision,
    recoverability double precision,
    protected boolean DEFAULT false NOT NULL,
    protection_reason text,
    first_flagged_at timestamp with time zone DEFAULT now() NOT NULL,
    last_evaluated_at timestamp with time zone DEFAULT now() NOT NULL,
    trash_after timestamp with time zone,
    resolver_version text DEFAULT 'semantic-retention-v1'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT semantic_retention_state_quality_score_check CHECK (((quality_score IS NULL) OR ((quality_score >= (0)::double precision) AND (quality_score <= (1)::double precision)))),
    CONSTRAINT semantic_retention_state_recoverability_check CHECK (((recoverability IS NULL) OR ((recoverability >= (0)::double precision) AND (recoverability <= (1)::double precision)))),
    CONSTRAINT semantic_retention_state_redundancy_score_check CHECK (((redundancy_score IS NULL) OR ((redundancy_score >= (0)::double precision) AND (redundancy_score <= (1)::double precision)))),
    CONSTRAINT semantic_retention_state_state_check CHECK ((state = ANY (ARRAY['ACTIVE'::text, 'COLD'::text, 'QUARANTINED'::text, 'TRASHED'::text]))),
    CONSTRAINT semantic_retention_state_utility_score_check CHECK (((utility_score IS NULL) OR ((utility_score >= (0)::double precision) AND (utility_score <= (1)::double precision))))
);


--
-- Name: TABLE semantic_retention_state; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_retention_state IS 'Logical retention state only. Absence means ACTIVE. Phase 1 never physically deletes source/evidence artifacts.';


--
-- Name: semantic_scope_projection_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_scope_projection_state (
    scope_key text NOT NULL,
    scope_kind text NOT NULL,
    rdf_dataset text,
    rdf_graph text,
    dirty_version bigint DEFAULT 0 NOT NULL,
    projected_version bigint DEFAULT 0 NOT NULL,
    status text DEFAULT 'dirty'::text NOT NULL,
    dirty_at timestamp with time zone,
    projected_at timestamp with time zone,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_scope_projection_state_dirty_version_check CHECK ((dirty_version >= 0)),
    CONSTRAINT semantic_scope_projection_state_projected_version_check CHECK ((projected_version >= 0)),
    CONSTRAINT semantic_scope_projection_state_status_check CHECK ((status = ANY (ARRAY['dirty'::text, 'projecting'::text, 'ready'::text, 'error'::text])))
);


--
-- Name: TABLE semantic_scope_projection_state; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_scope_projection_state IS 'Dirty/version ledger for coalesced semantic topology RDF projection. PostgreSQL topology is authoritative; Fuseki is a scope-level projection.';


--
-- Name: semantic_structure_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_structure_state (
    proposition_id uuid NOT NULL,
    embedding_version text NOT NULL,
    analyzed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: semantic_topology_edge; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_topology_edge (
    edge_id uuid DEFAULT gen_random_uuid() NOT NULL,
    scope_key text NOT NULL,
    parent_node_id uuid NOT NULL,
    child_node_id uuid NOT NULL,
    edge_type text NOT NULL,
    significance double precision DEFAULT 0.5 NOT NULL,
    claim_id uuid,
    assertion_id uuid,
    acquisition_id uuid,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    inference_source text DEFAULT 'deterministic'::text NOT NULL,
    inference_status text DEFAULT 'accepted'::text NOT NULL,
    inference_confidence double precision,
    CONSTRAINT semantic_topology_edge_check CHECK ((parent_node_id <> child_node_id)),
    CONSTRAINT semantic_topology_edge_inference_confidence_check CHECK (((inference_confidence IS NULL) OR ((inference_confidence >= (0)::double precision) AND (inference_confidence <= (1)::double precision)))),
    CONSTRAINT semantic_topology_edge_significance_check CHECK (((significance >= (0)::double precision) AND (significance <= (1)::double precision)))
);


--
-- Name: semantic_topology_node; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_topology_node (
    topology_node_id uuid DEFAULT gen_random_uuid() NOT NULL,
    scope_key text NOT NULL,
    scope_kind text NOT NULL,
    node_type text NOT NULL,
    node_key text NOT NULL,
    label text,
    character_id text,
    character_instance_id uuid,
    world_id uuid,
    source_id text,
    timeline_id uuid,
    dag_node_id uuid,
    proposition_id uuid,
    claim_id uuid,
    assertion_id uuid,
    acquisition_id uuid,
    significance double precision DEFAULT 0.5 NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_topology_node_scope_kind_check CHECK ((scope_kind = ANY (ARRAY['character'::text, 'world'::text, 'source'::text, 'unresolved'::text]))),
    CONSTRAINT semantic_topology_node_significance_check CHECK (((significance >= (0)::double precision) AND (significance <= (1)::double precision)))
);


--
-- Name: semantic_topology_projection; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_topology_projection (
    projection_key text NOT NULL,
    claim_id uuid,
    assertion_id uuid,
    acquisition_id uuid,
    scope_key text NOT NULL,
    rdf_dataset text NOT NULL,
    rdf_graph text NOT NULL,
    resolver_version text NOT NULL,
    projected_at timestamp with time zone,
    last_error text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT semantic_topology_projection_check CHECK (((claim_id IS NOT NULL) OR (assertion_id IS NOT NULL) OR (acquisition_id IS NOT NULL)))
);


--
-- Name: semantic_validation_decision; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_validation_decision (
    decision_type text NOT NULL,
    decision_key text NOT NULL,
    subject_type text NOT NULL,
    subject_key text NOT NULL,
    proposed_value text,
    selected_value text,
    status text NOT NULL,
    resolver_version text NOT NULL,
    winner_score integer DEFAULT 0 NOT NULL,
    runner_up_score integer DEFAULT 0 NOT NULL,
    margin integer DEFAULT 0 NOT NULL,
    stability double precision DEFAULT 0.0 NOT NULL,
    matrix jsonb DEFAULT '{}'::jsonb NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    evaluated_at timestamp with time zone DEFAULT now() NOT NULL,
    stale_at timestamp with time zone,
    CONSTRAINT semantic_validation_decision_stability_check CHECK (((stability >= (0)::double precision) AND (stability <= (1)::double precision))),
    CONSTRAINT semantic_validation_decision_status_check CHECK ((status = ANY (ARRAY['verified'::text, 'protected_explicit'::text, 'fragile'::text, 'challenged'::text, 'insufficient_evidence'::text, 'single_plausible_owner'::text, 'stale'::text])))
);


--
-- Name: TABLE semantic_validation_decision; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_validation_decision IS 'Revisable adversarial-matrix decisions. These rows describe derived interpretation, not source truth.';


--
-- Name: semantic_validation_dependency; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_validation_dependency (
    decision_type text NOT NULL,
    decision_key text NOT NULL,
    evidence_type text NOT NULL,
    evidence_key text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE semantic_validation_dependency; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.semantic_validation_dependency IS 'Explicit evidence dependency graph used for bounded recursive semantic invalidation when later evidence arrives.';


--
-- Name: semantic_vector_index_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.semantic_vector_index_state (
    object_type text NOT NULL,
    object_key text NOT NULL,
    qdrant_collection text NOT NULL,
    embedding_model text NOT NULL,
    embedding_version text NOT NULL,
    vector_hash text,
    indexed_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text
);


--
-- Name: session; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.session (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source text,
    source_session_id text,
    topic text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: source_document; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.source_document (
    document_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_type text NOT NULL,
    source_url text,
    title text,
    retrieved_at timestamp with time zone DEFAULT now() NOT NULL,
    raw_content text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: source_identity; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.source_identity (
    source_id text NOT NULL,
    source_kind text NOT NULL,
    display_name text,
    canonical_uri text,
    canonical_domain text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: timeline; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.timeline (
    timeline_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_id uuid NOT NULL,
    name text DEFAULT 'main'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    character_id text,
    user_name text,
    scope_key text DEFAULT 'default'::text NOT NULL,
    session_id uuid,
    source_id text
);


--
-- Name: user_identity; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.user_identity (
    user_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_key text,
    display_name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: world; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world (
    world_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_key text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    world_type text DEFAULT 'unknown'::text NOT NULL,
    parent_world_id uuid,
    canon_of_world_id uuid,
    root_world_id uuid,
    anchor_timeline_id uuid,
    anchor_node_id uuid,
    origin_character_id text
);


--
-- Name: world_entity; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_entity (
    entity_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_id uuid NOT NULL,
    entity_key text,
    entity_type text DEFAULT 'object'::text NOT NULL,
    display_name text,
    character_instance_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: world_entity_relation; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_entity_relation (
    relation_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_id uuid NOT NULL,
    subject_entity_id uuid NOT NULL,
    relation_type text NOT NULL,
    object_entity_id uuid NOT NULL,
    valid_from_node_id uuid,
    valid_to_node_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT world_entity_relation_check CHECK ((subject_entity_id <> object_entity_id))
);


--
-- Name: world_event; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_event (
    world_event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_id uuid NOT NULL,
    timeline_id uuid,
    instance_id uuid,
    actor_entity_id uuid,
    target_entity_id uuid,
    action_type text NOT NULL,
    status text DEFAULT 'accepted'::text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    dag_node_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    domain_id text,
    source_kind text,
    source_ref text,
    authority_kind text,
    candidate_id uuid,
    state_key text,
    before_state jsonb,
    delta jsonb,
    result_state jsonb,
    parent_state_version bigint,
    result_state_version bigint,
    occurred_at timestamp with time zone
);


--
-- Name: world_event_exposure; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_event_exposure (
    exposure_id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_id uuid NOT NULL,
    world_id uuid NOT NULL,
    world_timeline_id uuid NOT NULL,
    world_node_id uuid NOT NULL,
    character_timeline_id uuid NOT NULL,
    character_node_id uuid,
    acquisition_id uuid,
    exposure_kind text NOT NULL,
    confidence double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT world_event_exposure_confidence_check CHECK (((confidence IS NULL) OR ((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision)))),
    CONSTRAINT world_event_exposure_exposure_kind_check CHECK ((exposure_kind = ANY (ARRAY['observed'::text, 'participated'::text, 'caused'::text, 'heard_about'::text, 'inferred_from'::text, 'remembered_from'::text, 'sensor'::text, 'system'::text])))
);


--
-- Name: world_lineage; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_lineage (
    parent_world_id uuid NOT NULL,
    child_world_id uuid NOT NULL,
    split_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: world_memory_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_memory_state (
    world_id uuid NOT NULL,
    atom_id uuid NOT NULL,
    stance text NOT NULL,
    positive_support double precision DEFAULT 0.0 NOT NULL,
    negative_support double precision DEFAULT 0.0 NOT NULL,
    state_confidence double precision DEFAULT 0.0 NOT NULL,
    preferred_proposition_id uuid,
    preferred_assertion_id uuid,
    evidence_count integer DEFAULT 0 NOT NULL,
    independent_evidence_count integer DEFAULT 0 NOT NULL,
    resolved_through_node_id uuid,
    resolver_version text DEFAULT 'memory-reconciliation-v1'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT world_memory_state_evidence_count_check CHECK ((evidence_count >= 0)),
    CONSTRAINT world_memory_state_independent_evidence_count_check CHECK ((independent_evidence_count >= 0)),
    CONSTRAINT world_memory_state_negative_support_check CHECK (((negative_support >= (0)::double precision) AND (negative_support <= (1)::double precision))),
    CONSTRAINT world_memory_state_positive_support_check CHECK (((positive_support >= (0)::double precision) AND (positive_support <= (1)::double precision))),
    CONSTRAINT world_memory_state_stance_check CHECK ((stance = ANY (ARRAY['positive'::text, 'negative'::text, 'unresolved'::text]))),
    CONSTRAINT world_memory_state_state_confidence_check CHECK (((state_confidence >= (0)::double precision) AND (state_confidence <= (1)::double precision)))
);


--
-- Name: TABLE world_memory_state; Type: COMMENT; Schema: aios; Owner: -
--

COMMENT ON TABLE aios.world_memory_state IS 'Convergent current /world semantic memory. Only explicit world_proposition_assertion rows participate; /char evidence cannot promote itself into /world.';


--
-- Name: world_proposition_assertion; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_proposition_assertion (
    assertion_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_id uuid NOT NULL,
    proposition_id uuid NOT NULL,
    epistemic_status text DEFAULT 'tentative'::text NOT NULL,
    source_kind text DEFAULT 'observed'::text NOT NULL,
    confidence double precision DEFAULT 0.5 NOT NULL,
    generated_at_node_id uuid,
    reason text,
    superseded_by_assertion_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_checked_at timestamp with time zone,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: world_rdf_projection; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_rdf_projection (
    world_id uuid NOT NULL,
    rdf_graph text DEFAULT 'urn:aios:world:topology'::text NOT NULL,
    projected_at timestamp with time zone,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: world_rule; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_rule (
    rule_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_id uuid NOT NULL,
    rule_key text NOT NULL,
    rule_type text DEFAULT 'constraint'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    rule_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: world_split_candidate_world; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_split_candidate_world (
    split_id uuid NOT NULL,
    parent_world_id uuid NOT NULL,
    cluster_count integer NOT NULL,
    cluster_a jsonb NOT NULL,
    cluster_b jsonb NOT NULL,
    centroid_distance real NOT NULL,
    contradiction_density real NOT NULL,
    ambiguity_rate real NOT NULL,
    affinity_bimodality real NOT NULL,
    split_reason text NOT NULL,
    boundary_pairs jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: world_split_pressure; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_split_pressure (
    world_id uuid NOT NULL,
    claim_count integer NOT NULL,
    contradiction_density real NOT NULL,
    ambiguity_rate real NOT NULL,
    affinity_bimodality real NOT NULL,
    anchor_instability real NOT NULL,
    split_pressure real NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    window_start timestamp with time zone,
    window_end timestamp with time zone
);


--
-- Name: world_timeline_binding; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_timeline_binding (
    world_id uuid NOT NULL,
    objective_timeline_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: ingest_event event_id; Type: DEFAULT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.ingest_event ALTER COLUMN event_id SET DEFAULT nextval('aios.ingest_event_event_id_seq'::regclass);


--
-- Name: memory_item memory_id; Type: DEFAULT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_item ALTER COLUMN memory_id SET DEFAULT nextval('aios.memory_item_memory_id_seq'::regclass);


--
-- Name: belief_reconciliation_policy belief_reconciliation_policy_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.belief_reconciliation_policy
    ADD CONSTRAINT belief_reconciliation_policy_pkey PRIMARY KEY (policy_key);


--
-- Name: causal_admission causal_admission_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_admission
    ADD CONSTRAINT causal_admission_pkey PRIMARY KEY (admission_id);


--
-- Name: causal_candidate causal_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_pkey PRIMARY KEY (candidate_id);


--
-- Name: causal_state causal_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_state
    ADD CONSTRAINT causal_state_pkey PRIMARY KEY (world_id, timeline_id, domain_id, entity_id, state_key);


--
-- Name: character_alias character_alias_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_alias
    ADD CONSTRAINT character_alias_pkey PRIMARY KEY (alias);


--
-- Name: character_belief_state character_belief_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_belief_state
    ADD CONSTRAINT character_belief_state_pkey PRIMARY KEY (instance_id, atom_id);


--
-- Name: character_epistemic_profile character_epistemic_profile_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_epistemic_profile
    ADD CONSTRAINT character_epistemic_profile_pkey PRIMARY KEY (character_id);


--
-- Name: character_hud_profile character_hud_profile_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_profile
    ADD CONSTRAINT character_hud_profile_pkey PRIMARY KEY (character_id);


--
-- Name: character_hud_readiness character_hud_readiness_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_readiness
    ADD CONSTRAINT character_hud_readiness_pkey PRIMARY KEY (instance_id);


--
-- Name: character_identity character_identity_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_identity
    ADD CONSTRAINT character_identity_pkey PRIMARY KEY (character_id);


--
-- Name: character_instance character_instance_character_id_world_id_owner_user_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_character_id_world_id_owner_user_id_key UNIQUE (character_id, world_id, owner_user_id);


--
-- Name: character_instance character_instance_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_pkey PRIMARY KEY (instance_id);


--
-- Name: character_inventory character_inventory_instance_id_entity_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_inventory
    ADD CONSTRAINT character_inventory_instance_id_entity_id_key UNIQUE (instance_id, entity_id);


--
-- Name: character_inventory character_inventory_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_inventory
    ADD CONSTRAINT character_inventory_pkey PRIMARY KEY (inventory_id);


--
-- Name: character_knowledge character_knowledge_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_knowledge
    ADD CONSTRAINT character_knowledge_pkey PRIMARY KEY (instance_id, claim_id);


--
-- Name: character_proposition_knowledge character_proposition_knowledge_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_proposition_knowledge
    ADD CONSTRAINT character_proposition_knowledge_pkey PRIMARY KEY (instance_id, proposition_id);


--
-- Name: character_relationship character_relationship_observer_instance_id_target_entity_i_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_relationship
    ADD CONSTRAINT character_relationship_observer_instance_id_target_entity_i_key UNIQUE (observer_instance_id, target_entity_id);


--
-- Name: character_relationship character_relationship_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_relationship
    ADD CONSTRAINT character_relationship_pkey PRIMARY KEY (relationship_id);


--
-- Name: character_runtime_state character_runtime_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_runtime_state
    ADD CONSTRAINT character_runtime_state_pkey PRIMARY KEY (instance_id);


--
-- Name: character_world_timeline_link character_world_timeline_link_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_world_timeline_link
    ADD CONSTRAINT character_world_timeline_link_pkey PRIMARY KEY (character_timeline_id);


--
-- Name: claim_candidate claim_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_candidate
    ADD CONSTRAINT claim_candidate_pkey PRIMARY KEY (claim_id);


--
-- Name: claim_context_resolution claim_context_resolution_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_context_resolution
    ADD CONSTRAINT claim_context_resolution_pkey PRIMARY KEY (claim_id);


--
-- Name: claim_contradiction_candidate claim_contradiction_candidate_claim_a_id_claim_b_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_contradiction_candidate
    ADD CONSTRAINT claim_contradiction_candidate_claim_a_id_claim_b_id_key UNIQUE (claim_a_id, claim_b_id);


--
-- Name: claim_contradiction_candidate claim_contradiction_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_contradiction_candidate
    ADD CONSTRAINT claim_contradiction_candidate_pkey PRIMARY KEY (contradiction_id);


--
-- Name: claim_provenance claim_provenance_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_provenance
    ADD CONSTRAINT claim_provenance_pkey PRIMARY KEY (claim_id, document_id);


--
-- Name: claim_semantic_frame claim_semantic_frame_claim_id_frame_index_decomposer_versio_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_semantic_frame
    ADD CONSTRAINT claim_semantic_frame_claim_id_frame_index_decomposer_versio_key UNIQUE (claim_id, frame_index, decomposer_version);


--
-- Name: claim_semantic_frame claim_semantic_frame_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_semantic_frame
    ADD CONSTRAINT claim_semantic_frame_pkey PRIMARY KEY (frame_id);


--
-- Name: claim_semantic_frame_projection claim_semantic_frame_projection_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_semantic_frame_projection
    ADD CONSTRAINT claim_semantic_frame_projection_pkey PRIMARY KEY (claim_id);


--
-- Name: claim_similarity_edge claim_similarity_edge_claim_a_id_claim_b_id_embedding_model_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_similarity_edge
    ADD CONSTRAINT claim_similarity_edge_claim_a_id_claim_b_id_embedding_model_key UNIQUE (claim_a_id, claim_b_id, embedding_model, embedding_version);


--
-- Name: claim_world_assignment claim_world_assignment_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_world_assignment
    ADD CONSTRAINT claim_world_assignment_pkey PRIMARY KEY (claim_id);


--
-- Name: dag_edge dag_edge_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.dag_edge
    ADD CONSTRAINT dag_edge_pkey PRIMARY KEY (timeline_id, parent_node_id, child_node_id);


--
-- Name: dag_node dag_node_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.dag_node
    ADD CONSTRAINT dag_node_pkey PRIMARY KEY (node_id);


--
-- Name: dag_node dag_node_timeline_id_event_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.dag_node
    ADD CONSTRAINT dag_node_timeline_id_event_id_key UNIQUE (timeline_id, event_id);


--
-- Name: document_metadata_observation document_metadata_observation_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_metadata_observation
    ADD CONSTRAINT document_metadata_observation_pkey PRIMARY KEY (metadata_id);


--
-- Name: document_section document_section_node_id_unique; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_section
    ADD CONSTRAINT document_section_node_id_unique UNIQUE (node_id);


--
-- Name: document_section document_section_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_section
    ADD CONSTRAINT document_section_pkey PRIMARY KEY (section_id);


--
-- Name: document_unit document_unit_document_id_path_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_unit
    ADD CONSTRAINT document_unit_document_id_path_key UNIQUE (document_id, path);


--
-- Name: document_unit document_unit_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_unit
    ADD CONSTRAINT document_unit_pkey PRIMARY KEY (unit_id);


--
-- Name: entity_controller entity_controller_entity_id_controller_type_controller_ref_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.entity_controller
    ADD CONSTRAINT entity_controller_entity_id_controller_type_controller_ref_key UNIQUE (entity_id, controller_type, controller_ref);


--
-- Name: entity_controller entity_controller_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.entity_controller
    ADD CONSTRAINT entity_controller_pkey PRIMARY KEY (controller_id);


--
-- Name: extracted_sentence extracted_sentence_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.extracted_sentence
    ADD CONSTRAINT extracted_sentence_pkey PRIMARY KEY (sentence_id);


--
-- Name: extracted_sentence extracted_sentence_section_id_sentence_index_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.extracted_sentence
    ADD CONSTRAINT extracted_sentence_section_id_sentence_index_key UNIQUE (section_id, sentence_index);


--
-- Name: hud_profile hud_profile_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.hud_profile
    ADD CONSTRAINT hud_profile_pkey PRIMARY KEY (profile_id);


--
-- Name: hud_profile hud_profile_profile_name_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.hud_profile
    ADD CONSTRAINT hud_profile_profile_name_key UNIQUE (profile_name);


--
-- Name: ingest_event ingest_event_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.ingest_event
    ADD CONSTRAINT ingest_event_pkey PRIMARY KEY (event_id);


--
-- Name: knowledge_acquisition_event knowledge_acquisition_event_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.knowledge_acquisition_event
    ADD CONSTRAINT knowledge_acquisition_event_pkey PRIMARY KEY (acquisition_id);


--
-- Name: memory_item memory_item_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_item
    ADD CONSTRAINT memory_item_pkey PRIMARY KEY (memory_id);


--
-- Name: memory_reconciliation_receipt memory_reconciliation_receipt_assertion_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_reconciliation_receipt
    ADD CONSTRAINT memory_reconciliation_receipt_assertion_id_key UNIQUE (assertion_id);


--
-- Name: memory_reconciliation_receipt memory_reconciliation_receipt_claim_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_reconciliation_receipt
    ADD CONSTRAINT memory_reconciliation_receipt_claim_id_key UNIQUE (claim_id);


--
-- Name: memory_reconciliation_receipt memory_reconciliation_receipt_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_reconciliation_receipt
    ADD CONSTRAINT memory_reconciliation_receipt_pkey PRIMARY KEY (receipt_id);


--
-- Name: message_cognitive_commit message_cognitive_commit_instance_id_node_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.message_cognitive_commit
    ADD CONSTRAINT message_cognitive_commit_instance_id_node_id_key UNIQUE (instance_id, node_id);


--
-- Name: message_cognitive_commit message_cognitive_commit_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.message_cognitive_commit
    ADD CONSTRAINT message_cognitive_commit_pkey PRIMARY KEY (commit_id);


--
-- Name: message_cognitive_unit message_cognitive_unit_commit_id_ordinal_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.message_cognitive_unit
    ADD CONSTRAINT message_cognitive_unit_commit_id_ordinal_key UNIQUE (commit_id, ordinal);


--
-- Name: message_cognitive_unit message_cognitive_unit_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.message_cognitive_unit
    ADD CONSTRAINT message_cognitive_unit_pkey PRIMARY KEY (unit_id);


--
-- Name: narrative_cluster narrative_cluster_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.narrative_cluster
    ADD CONSTRAINT narrative_cluster_pkey PRIMARY KEY (narrative_id);


--
-- Name: narrative_cluster narrative_cluster_topic_key_narrative_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.narrative_cluster
    ADD CONSTRAINT narrative_cluster_topic_key_narrative_key_key UNIQUE (topic_key, narrative_key);


--
-- Name: narrative_membership narrative_membership_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.narrative_membership
    ADD CONSTRAINT narrative_membership_pkey PRIMARY KEY (narrative_id, observation_id);


--
-- Name: narrative_source_affinity narrative_source_affinity_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.narrative_source_affinity
    ADD CONSTRAINT narrative_source_affinity_pkey PRIMARY KEY (narrative_id, source_key);


--
-- Name: observation observation_claim_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation
    ADD CONSTRAINT observation_claim_id_key UNIQUE (claim_id);


--
-- Name: observation observation_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation
    ADD CONSTRAINT observation_pkey PRIMARY KEY (observation_id);


--
-- Name: observation_proposition observation_proposition_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation_proposition
    ADD CONSTRAINT observation_proposition_pkey PRIMARY KEY (observation_id, frame_id);


--
-- Name: pipeline_job pipeline_job_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.pipeline_job
    ADD CONSTRAINT pipeline_job_pkey PRIMARY KEY (job_id);


--
-- Name: pipeline_stage_config pipeline_stage_config_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.pipeline_stage_config
    ADD CONSTRAINT pipeline_stage_config_pkey PRIMARY KEY (stage_name);


--
-- Name: proposition_conflict proposition_conflict_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition_conflict
    ADD CONSTRAINT proposition_conflict_pkey PRIMARY KEY (conflict_id);


--
-- Name: proposition_evidence proposition_evidence_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition_evidence
    ADD CONSTRAINT proposition_evidence_pkey PRIMARY KEY (evidence_id);


--
-- Name: proposition_evidence proposition_evidence_proposition_id_observation_id_evidence_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition_evidence
    ADD CONSTRAINT proposition_evidence_proposition_id_observation_id_evidence_key UNIQUE (proposition_id, observation_id, evidence_role);


--
-- Name: proposition proposition_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition
    ADD CONSTRAINT proposition_pkey PRIMARY KEY (proposition_id);


--
-- Name: proposition proposition_proposition_hash_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition
    ADD CONSTRAINT proposition_proposition_hash_key UNIQUE (proposition_hash);


--
-- Name: rdf_promotion_log rdf_promotion_log_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.rdf_promotion_log
    ADD CONSTRAINT rdf_promotion_log_pkey PRIMARY KEY (promotion_id);


--
-- Name: reconciliation_family_policy reconciliation_family_policy_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.reconciliation_family_policy
    ADD CONSTRAINT reconciliation_family_policy_pkey PRIMARY KEY (predicate_family);


--
-- Name: retention_event retention_event_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.retention_event
    ADD CONSTRAINT retention_event_pkey PRIMARY KEY (event_id);


--
-- Name: semantic_anchor_edge semantic_anchor_edge_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_pkey PRIMARY KEY (anchor_edge_id);


--
-- Name: semantic_anchor_edge semantic_anchor_edge_source_node_id_target_node_id_relation_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_source_node_id_target_node_id_relation_key UNIQUE (source_node_id, target_node_id, relationship_type);


--
-- Name: semantic_atom semantic_atom_atom_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_atom
    ADD CONSTRAINT semantic_atom_atom_key_key UNIQUE (atom_key);


--
-- Name: semantic_atom semantic_atom_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_atom
    ADD CONSTRAINT semantic_atom_pkey PRIMARY KEY (atom_id);


--
-- Name: semantic_boundary_classification semantic_boundary_classificat_run_id_cluster_a_id_cluster_b_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_boundary_classification
    ADD CONSTRAINT semantic_boundary_classificat_run_id_cluster_a_id_cluster_b_key UNIQUE (run_id, cluster_a_id, cluster_b_id, classifier_version);


--
-- Name: semantic_boundary_classification semantic_boundary_classification_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_boundary_classification
    ADD CONSTRAINT semantic_boundary_classification_pkey PRIMARY KEY (classification_id);


--
-- Name: semantic_branch_candidate semantic_branch_candidate_boundary_classification_id_scope__key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_boundary_classification_id_scope__key UNIQUE (boundary_classification_id, scope_partition_key, candidate_kind);


--
-- Name: semantic_branch_candidate semantic_branch_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_pkey PRIMARY KEY (branch_candidate_id);


--
-- Name: semantic_cluster_boundary semantic_cluster_boundary_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_boundary
    ADD CONSTRAINT semantic_cluster_boundary_pkey PRIMARY KEY (run_id, cluster_a_id, cluster_b_id);


--
-- Name: semantic_cluster_candidate semantic_cluster_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_candidate
    ADD CONSTRAINT semantic_cluster_candidate_pkey PRIMARY KEY (cluster_id);


--
-- Name: semantic_cluster_candidate semantic_cluster_candidate_run_id_cluster_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_candidate
    ADD CONSTRAINT semantic_cluster_candidate_run_id_cluster_key_key UNIQUE (run_id, cluster_key);


--
-- Name: semantic_cluster_classification semantic_cluster_classificati_run_id_cluster_id_classifier__key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_classification
    ADD CONSTRAINT semantic_cluster_classificati_run_id_cluster_id_classifier__key UNIQUE (run_id, cluster_id, classifier_version);


--
-- Name: semantic_cluster_classification semantic_cluster_classification_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_classification
    ADD CONSTRAINT semantic_cluster_classification_pkey PRIMARY KEY (classification_id);


--
-- Name: semantic_cluster_membership semantic_cluster_membership_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_membership
    ADD CONSTRAINT semantic_cluster_membership_pkey PRIMARY KEY (cluster_id, proposition_id);


--
-- Name: semantic_cluster_run semantic_cluster_run_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_run
    ADD CONSTRAINT semantic_cluster_run_pkey PRIMARY KEY (run_id);


--
-- Name: semantic_evidence_admission semantic_evidence_admission_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_evidence_admission
    ADD CONSTRAINT semantic_evidence_admission_pkey PRIMARY KEY (acquisition_id);


--
-- Name: semantic_interpretation semantic_interpretation_frame_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_interpretation
    ADD CONSTRAINT semantic_interpretation_frame_id_key UNIQUE (frame_id);


--
-- Name: semantic_interpretation semantic_interpretation_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_interpretation
    ADD CONSTRAINT semantic_interpretation_pkey PRIMARY KEY (interpretation_id);


--
-- Name: semantic_interpretation_role semantic_interpretation_role_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_interpretation_role
    ADD CONSTRAINT semantic_interpretation_role_pkey PRIMARY KEY (interpretation_id, role_name, ordinal);


--
-- Name: semantic_neighbor_candidate semantic_neighbor_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_neighbor_candidate
    ADD CONSTRAINT semantic_neighbor_candidate_pkey PRIMARY KEY (proposition_id, neighbor_proposition_id, embedding_version);


--
-- Name: semantic_neighbor_relation semantic_neighbor_relation_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_neighbor_relation
    ADD CONSTRAINT semantic_neighbor_relation_pkey PRIMARY KEY (proposition_id, neighbor_proposition_id, embedding_version, classifier_version);


--
-- Name: semantic_outlier_candidate semantic_outlier_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_outlier_candidate
    ADD CONSTRAINT semantic_outlier_candidate_pkey PRIMARY KEY (run_id, proposition_id);


--
-- Name: semantic_reconciliation_receipt semantic_reconciliation_receipt_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_reconciliation_receipt
    ADD CONSTRAINT semantic_reconciliation_receipt_pkey PRIMARY KEY (receipt_id);


--
-- Name: semantic_reconciliation_receipt semantic_reconciliation_receipt_receipt_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_reconciliation_receipt
    ADD CONSTRAINT semantic_reconciliation_receipt_receipt_key_key UNIQUE (receipt_key);


--
-- Name: semantic_retention_state semantic_retention_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_retention_state
    ADD CONSTRAINT semantic_retention_state_pkey PRIMARY KEY (artifact_type, artifact_id);


--
-- Name: semantic_scope_projection_state semantic_scope_projection_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_scope_projection_state
    ADD CONSTRAINT semantic_scope_projection_state_pkey PRIMARY KEY (scope_key);


--
-- Name: semantic_structure_state semantic_structure_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_structure_state
    ADD CONSTRAINT semantic_structure_state_pkey PRIMARY KEY (proposition_id);


--
-- Name: semantic_topology_edge semantic_topology_edge_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_edge
    ADD CONSTRAINT semantic_topology_edge_pkey PRIMARY KEY (edge_id);


--
-- Name: semantic_topology_edge semantic_topology_edge_scope_key_parent_node_id_child_node__key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_edge
    ADD CONSTRAINT semantic_topology_edge_scope_key_parent_node_id_child_node__key UNIQUE (scope_key, parent_node_id, child_node_id, edge_type);


--
-- Name: semantic_topology_node semantic_topology_node_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_pkey PRIMARY KEY (topology_node_id);


--
-- Name: semantic_topology_node semantic_topology_node_scope_key_node_type_node_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_scope_key_node_type_node_key_key UNIQUE (scope_key, node_type, node_key);


--
-- Name: semantic_topology_projection semantic_topology_projection_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_projection
    ADD CONSTRAINT semantic_topology_projection_pkey PRIMARY KEY (projection_key);


--
-- Name: semantic_validation_decision semantic_validation_decision_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_validation_decision
    ADD CONSTRAINT semantic_validation_decision_pkey PRIMARY KEY (decision_type, decision_key);


--
-- Name: semantic_validation_dependency semantic_validation_dependency_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_validation_dependency
    ADD CONSTRAINT semantic_validation_dependency_pkey PRIMARY KEY (decision_type, decision_key, evidence_type, evidence_key);


--
-- Name: semantic_vector_index_state semantic_vector_index_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_vector_index_state
    ADD CONSTRAINT semantic_vector_index_state_pkey PRIMARY KEY (object_type, object_key, qdrant_collection, embedding_model, embedding_version);


--
-- Name: session session_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.session
    ADD CONSTRAINT session_pkey PRIMARY KEY (session_id);


--
-- Name: source_document source_document_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.source_document
    ADD CONSTRAINT source_document_pkey PRIMARY KEY (document_id);


--
-- Name: source_identity source_identity_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.source_identity
    ADD CONSTRAINT source_identity_pkey PRIMARY KEY (source_id);


--
-- Name: timeline timeline_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.timeline
    ADD CONSTRAINT timeline_pkey PRIMARY KEY (timeline_id);


--
-- Name: user_identity user_identity_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.user_identity
    ADD CONSTRAINT user_identity_pkey PRIMARY KEY (user_id);


--
-- Name: user_identity user_identity_user_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.user_identity
    ADD CONSTRAINT user_identity_user_key_key UNIQUE (user_key);


--
-- Name: ingest_event ux_ingest_event_dedupe_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.ingest_event
    ADD CONSTRAINT ux_ingest_event_dedupe_key UNIQUE (dedupe_key);


--
-- Name: world_entity world_entity_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity
    ADD CONSTRAINT world_entity_pkey PRIMARY KEY (entity_id);


--
-- Name: world_entity_relation world_entity_relation_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity_relation
    ADD CONSTRAINT world_entity_relation_pkey PRIMARY KEY (relation_id);


--
-- Name: world_entity world_entity_world_id_entity_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity
    ADD CONSTRAINT world_entity_world_id_entity_key_key UNIQUE (world_id, entity_key);


--
-- Name: world_event_exposure world_event_exposure_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event_exposure
    ADD CONSTRAINT world_event_exposure_pkey PRIMARY KEY (exposure_id);


--
-- Name: world_event world_event_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event
    ADD CONSTRAINT world_event_pkey PRIMARY KEY (world_event_id);


--
-- Name: world_lineage world_lineage_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_lineage
    ADD CONSTRAINT world_lineage_pkey PRIMARY KEY (parent_world_id, child_world_id);


--
-- Name: world_memory_state world_memory_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_memory_state
    ADD CONSTRAINT world_memory_state_pkey PRIMARY KEY (world_id, atom_id);


--
-- Name: world world_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_pkey PRIMARY KEY (world_id);


--
-- Name: world_proposition_assertion world_proposition_assertion_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_proposition_assertion
    ADD CONSTRAINT world_proposition_assertion_pkey PRIMARY KEY (assertion_id);


--
-- Name: world_proposition_assertion world_proposition_assertion_world_id_proposition_id_source__key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_proposition_assertion
    ADD CONSTRAINT world_proposition_assertion_world_id_proposition_id_source__key UNIQUE (world_id, proposition_id, source_kind);


--
-- Name: world_rdf_projection world_rdf_projection_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_rdf_projection
    ADD CONSTRAINT world_rdf_projection_pkey PRIMARY KEY (world_id);


--
-- Name: world_rule world_rule_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_rule
    ADD CONSTRAINT world_rule_pkey PRIMARY KEY (rule_id);


--
-- Name: world_rule world_rule_world_id_rule_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_rule
    ADD CONSTRAINT world_rule_world_id_rule_key_key UNIQUE (world_id, rule_key);


--
-- Name: world_split_candidate_world world_split_candidate_world_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_split_candidate_world
    ADD CONSTRAINT world_split_candidate_world_pkey PRIMARY KEY (split_id);


--
-- Name: world_split_pressure world_split_pressure_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_split_pressure
    ADD CONSTRAINT world_split_pressure_pkey PRIMARY KEY (world_id);


--
-- Name: world_timeline_binding world_timeline_binding_objective_timeline_id_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_timeline_binding
    ADD CONSTRAINT world_timeline_binding_objective_timeline_id_key UNIQUE (objective_timeline_id);


--
-- Name: world_timeline_binding world_timeline_binding_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_timeline_binding
    ADD CONSTRAINT world_timeline_binding_pkey PRIMARY KEY (world_id);


--
-- Name: world world_world_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_world_key_key UNIQUE (world_key);


--
-- Name: extracted_sentence_section_id_idx; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX extracted_sentence_section_id_idx ON aios.extracted_sentence USING btree (section_id);


--
-- Name: idx_causal_admission_candidate; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_causal_admission_candidate ON aios.causal_admission USING btree (candidate_id, created_at DESC);


--
-- Name: idx_causal_admission_decision; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_causal_admission_decision ON aios.causal_admission USING btree (decision, created_at DESC);


--
-- Name: idx_causal_candidate_claim; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_causal_candidate_claim ON aios.causal_candidate USING btree (claim_id) WHERE (claim_id IS NOT NULL);


--
-- Name: idx_causal_candidate_coordinate; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_causal_candidate_coordinate ON aios.causal_candidate USING btree (world_id, timeline_id, domain_id, entity_id, state_key, created_at);


--
-- Name: idx_causal_state_timeline; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_causal_state_timeline ON aios.causal_state USING btree (timeline_id, domain_id, entity_id);


--
-- Name: idx_char_instance_lookup; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_char_instance_lookup ON aios.character_instance USING btree (character_id, world_id, owner_user_id);


--
-- Name: idx_character_alias_character; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_alias_character ON aios.character_alias USING btree (character_id);


--
-- Name: idx_character_belief_state_instance; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_belief_state_instance ON aios.character_belief_state USING btree (instance_id, stance, belief_confidence DESC);


--
-- Name: idx_character_hud_readiness_live; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_hud_readiness_live ON aios.character_hud_readiness USING btree (live, status, updated_at DESC);


--
-- Name: idx_character_hud_readiness_source; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_hud_readiness_source ON aios.character_hud_readiness USING btree (source_timeline_id, source_head_event_id);


--
-- Name: idx_character_identity_canon; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_identity_canon ON aios.character_identity USING btree (canon);


--
-- Name: idx_character_identity_franchise; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_identity_franchise ON aios.character_identity USING btree (franchise);


--
-- Name: idx_character_runtime_source_head; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_runtime_source_head ON aios.character_runtime_state USING btree (source_head_node_id);


--
-- Name: idx_character_runtime_source_timeline; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_runtime_source_timeline ON aios.character_runtime_state USING btree (source_timeline_id);


--
-- Name: idx_character_runtime_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_runtime_world ON aios.character_runtime_state USING btree (world_id, lifecycle_state);


--
-- Name: idx_character_world_timeline_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_world_timeline_world ON aios.character_world_timeline_link USING btree (world_id, world_timeline_id);


--
-- Name: idx_claim_candidate_bucket; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_candidate_bucket ON aios.claim_candidate USING btree (claim_bucket);


--
-- Name: idx_claim_candidate_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_candidate_status ON aios.claim_candidate USING btree (status);


--
-- Name: idx_claim_candidate_subject; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_candidate_subject ON aios.claim_candidate USING btree (subject);


--
-- Name: idx_claim_context_character; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_context_character ON aios.claim_context_resolution USING btree (origin_character_id, resolved_at DESC) WHERE (origin_character_id IS NOT NULL);


--
-- Name: idx_claim_context_instance; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_context_instance ON aios.claim_context_resolution USING btree (character_instance_id, resolved_at DESC) WHERE (character_instance_id IS NOT NULL);


--
-- Name: idx_claim_context_semantics; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_context_semantics ON aios.claim_context_resolution USING btree (claim_kind, predicate_family);


--
-- Name: idx_claim_context_source; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_context_source ON aios.claim_context_resolution USING btree (source_id, resolved_at DESC) WHERE (source_id IS NOT NULL);


--
-- Name: idx_claim_context_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_context_world ON aios.claim_context_resolution USING btree (world_id, resolved_at DESC) WHERE (world_id IS NOT NULL);


--
-- Name: idx_claim_semantic_frame_canonical; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_semantic_frame_canonical ON aios.claim_semantic_frame USING btree (claim_id, predicate_canonical);


--
-- Name: idx_claim_semantic_frame_claim; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_semantic_frame_claim ON aios.claim_semantic_frame USING btree (claim_id, frame_index);


--
-- Name: idx_claim_semantic_frame_object_entity; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_semantic_frame_object_entity ON aios.claim_semantic_frame USING btree (object_entity_key) WHERE (object_entity_key IS NOT NULL);


--
-- Name: idx_claim_semantic_frame_subject_entity; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_semantic_frame_subject_entity ON aios.claim_semantic_frame USING btree (subject_entity_key) WHERE (subject_entity_key IS NOT NULL);


--
-- Name: idx_dag_edge_child; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_dag_edge_child ON aios.dag_edge USING btree (timeline_id, child_node_id);


--
-- Name: idx_dag_edge_parent; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_dag_edge_parent ON aios.dag_edge USING btree (timeline_id, parent_node_id);


--
-- Name: idx_dag_node_character; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_dag_node_character ON aios.dag_node USING btree (character_id, created_at DESC);


--
-- Name: idx_dag_node_kind_created; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_dag_node_kind_created ON aios.dag_node USING btree (kind, created_at);


--
-- Name: idx_dag_node_timeline; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_dag_node_timeline ON aios.dag_node USING btree (timeline_id, created_at DESC);


--
-- Name: idx_dag_node_timeline_event; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_dag_node_timeline_event ON aios.dag_node USING btree (timeline_id, event_id DESC);


--
-- Name: idx_document_metadata_doc_field; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_document_metadata_doc_field ON aios.document_metadata_observation USING btree (document_id, field_type);


--
-- Name: idx_document_section_document; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_document_section_document ON aios.document_section USING btree (document_id);


--
-- Name: idx_document_unit_doc_type; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_document_unit_doc_type ON aios.document_unit USING btree (document_id, unit_type, unit_index);


--
-- Name: idx_ingest_event_active_source_slot; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_active_source_slot ON aios.ingest_event USING btree (session_id, source, source_event_id, event_id DESC) WHERE ((superseded_at IS NULL) AND (source_event_id IS NOT NULL));


--
-- Name: idx_ingest_event_character; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_character ON aios.ingest_event USING btree (character_id, created_at DESC);


--
-- Name: idx_ingest_event_created_at; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_created_at ON aios.ingest_event USING btree (created_at DESC);


--
-- Name: idx_ingest_event_kind; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_kind ON aios.ingest_event USING btree (kind, created_at DESC);


--
-- Name: idx_ingest_event_payload_gin; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_payload_gin ON aios.ingest_event USING gin (payload);


--
-- Name: idx_ingest_event_pipeline_state; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_pipeline_state ON aios.ingest_event USING btree (process_status, rdf_processed_at, created_at DESC);


--
-- Name: idx_ingest_event_session_time; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_session_time ON aios.ingest_event USING btree (session_id, created_at DESC);


--
-- Name: idx_ingest_event_source_identity; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_source_identity ON aios.ingest_event USING btree (source_id, created_at DESC) WHERE (source_id IS NOT NULL);


--
-- Name: idx_ingest_event_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_status ON aios.ingest_event USING btree (process_status, created_at DESC);


--
-- Name: idx_ingest_event_superseded; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_superseded ON aios.ingest_event USING btree (superseded_at, event_id) WHERE (superseded_at IS NOT NULL);


--
-- Name: idx_ingest_event_target_character; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_target_character ON aios.ingest_event USING btree (target_character_id, created_at DESC) WHERE (target_character_id IS NOT NULL);


--
-- Name: idx_ingest_event_target_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_target_world ON aios.ingest_event USING btree (target_world_id, created_at DESC) WHERE (target_world_id IS NOT NULL);


--
-- Name: idx_knowledge_acquisition_pending; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_knowledge_acquisition_pending ON aios.knowledge_acquisition_event USING btree (created_at) WHERE (processed_at IS NULL);


--
-- Name: idx_memory_reconciliation_receipt_scope; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_memory_reconciliation_receipt_scope ON aios.memory_reconciliation_receipt USING btree (path, scope_key, updated_at DESC);


--
-- Name: idx_memory_thread_root; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_memory_thread_root ON aios.memory_item USING btree (thread_root_node_id);


--
-- Name: idx_memory_timeline_created; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_memory_timeline_created ON aios.memory_item USING btree (timeline_id, created_at DESC);


--
-- Name: idx_message_cognitive_commit_instance_event; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_message_cognitive_commit_instance_event ON aios.message_cognitive_commit USING btree (instance_id, event_id DESC);


--
-- Name: idx_message_cognitive_commit_node; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_message_cognitive_commit_node ON aios.message_cognitive_commit USING btree (node_id);


--
-- Name: idx_message_cognitive_unit_commit_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_message_cognitive_unit_commit_status ON aios.message_cognitive_unit USING btree (commit_id, status, salience DESC);


--
-- Name: idx_message_cognitive_unit_topic; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_message_cognitive_unit_topic ON aios.message_cognitive_unit USING btree (topic_key, claim_kind, created_at DESC);


--
-- Name: idx_narrative_membership_observation; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_narrative_membership_observation ON aios.narrative_membership USING btree (observation_id);


--
-- Name: idx_observation_proposition; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_observation_proposition ON aios.observation USING btree (proposition_id, observed_at DESC);


--
-- Name: idx_observation_proposition_proposition; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_observation_proposition_proposition ON aios.observation_proposition USING btree (proposition_id, observation_id);


--
-- Name: idx_observation_source; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_observation_source ON aios.observation USING btree (source_domain, observed_at DESC);


--
-- Name: idx_pipeline_job_pick; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_pipeline_job_pick ON aios.pipeline_job USING btree (status, priority, run_after, created_at);


--
-- Name: idx_proposition_atom; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_proposition_atom ON aios.proposition USING btree (atom_id);


--
-- Name: idx_proposition_topic; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_proposition_topic ON aios.proposition USING btree (topic_key);


--
-- Name: idx_rdf_promotion_claim; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_rdf_promotion_claim ON aios.rdf_promotion_log USING btree (claim_id);


--
-- Name: idx_retention_event_artifact; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_retention_event_artifact ON aios.retention_event USING btree (artifact_type, artifact_id, created_at DESC);


--
-- Name: idx_semantic_anchor_acquisition; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_anchor_acquisition ON aios.semantic_anchor_edge USING btree (acquisition_id) WHERE (acquisition_id IS NOT NULL);


--
-- Name: idx_semantic_anchor_character; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_anchor_character ON aios.semantic_anchor_edge USING btree (character_id, character_instance_id) WHERE (character_id IS NOT NULL);


--
-- Name: idx_semantic_anchor_source; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_anchor_source ON aios.semantic_anchor_edge USING btree (source_scope_key, relationship_type, source_node_id);


--
-- Name: idx_semantic_anchor_target; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_anchor_target ON aios.semantic_anchor_edge USING btree (target_scope_key, relationship_type, target_node_id);


--
-- Name: idx_semantic_anchor_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_anchor_world ON aios.semantic_anchor_edge USING btree (world_id, proposition_id) WHERE (world_id IS NOT NULL);


--
-- Name: idx_semantic_boundary_classification_type; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_boundary_classification_type ON aios.semantic_boundary_classification USING btree (classification, confidence DESC, created_at DESC);


--
-- Name: idx_semantic_branch_candidate_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_branch_candidate_status ON aios.semantic_branch_candidate USING btree (candidate_kind, status, confidence DESC);


--
-- Name: idx_semantic_cluster_boundary_strength; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_cluster_boundary_strength ON aios.semantic_cluster_boundary USING btree (run_id, max_similarity DESC);


--
-- Name: idx_semantic_cluster_candidate_key; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_cluster_candidate_key ON aios.semantic_cluster_candidate USING btree (cluster_key, created_at DESC);


--
-- Name: idx_semantic_cluster_candidate_run; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_cluster_candidate_run ON aios.semantic_cluster_candidate USING btree (run_id, cohesion DESC);


--
-- Name: idx_semantic_cluster_candidate_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_cluster_candidate_status ON aios.semantic_cluster_candidate USING btree (embedding_version, status, cohesion DESC);


--
-- Name: idx_semantic_cluster_classification_type; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_cluster_classification_type ON aios.semantic_cluster_classification USING btree (classification, confidence DESC, created_at DESC);


--
-- Name: idx_semantic_cluster_membership_proposition; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_cluster_membership_proposition ON aios.semantic_cluster_membership USING btree (proposition_id, affinity DESC);


--
-- Name: idx_semantic_cluster_run_latest; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_cluster_run_latest ON aios.semantic_cluster_run USING btree (embedding_version, completed_at DESC);


--
-- Name: idx_semantic_evidence_admission_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_evidence_admission_status ON aios.semantic_evidence_admission USING btree (status, updated_at DESC);


--
-- Name: idx_semantic_neighbor_relation_type; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_neighbor_relation_type ON aios.semantic_neighbor_relation USING btree (embedding_version, relation, confidence DESC);


--
-- Name: idx_semantic_outlier_candidate_prop; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_outlier_candidate_prop ON aios.semantic_outlier_candidate USING btree (proposition_id, created_at DESC);


--
-- Name: idx_semantic_reconciliation_scope; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_reconciliation_scope ON aios.semantic_reconciliation_receipt USING btree (scope_partition_key, source_kind, reconciled_at DESC);


--
-- Name: idx_semantic_retention_state_scope; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_retention_state_scope ON aios.semantic_retention_state USING btree (scope_key, state) WHERE (scope_key IS NOT NULL);


--
-- Name: idx_semantic_retention_state_state; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_retention_state_state ON aios.semantic_retention_state USING btree (state, last_evaluated_at DESC);


--
-- Name: idx_semantic_scope_projection_dirty; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_scope_projection_dirty ON aios.semantic_scope_projection_state USING btree (status, dirty_version, projected_version, updated_at) WHERE (dirty_version > projected_version);


--
-- Name: idx_semantic_topology_character; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_character ON aios.semantic_topology_node USING btree (character_id, created_at DESC) WHERE (character_id IS NOT NULL);


--
-- Name: idx_semantic_topology_edge_child; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_edge_child ON aios.semantic_topology_edge USING btree (scope_key, child_node_id, edge_type);


--
-- Name: idx_semantic_topology_edge_inference; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_edge_inference ON aios.semantic_topology_edge USING btree (scope_key, inference_source, inference_status, edge_type);


--
-- Name: idx_semantic_topology_edge_parent; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_edge_parent ON aios.semantic_topology_edge USING btree (scope_key, parent_node_id, edge_type);


--
-- Name: idx_semantic_topology_projection_acquisition; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_projection_acquisition ON aios.semantic_topology_projection USING btree (acquisition_id) WHERE (acquisition_id IS NOT NULL);


--
-- Name: idx_semantic_topology_projection_assertion; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_projection_assertion ON aios.semantic_topology_projection USING btree (assertion_id) WHERE (assertion_id IS NOT NULL);


--
-- Name: idx_semantic_topology_projection_claim; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_projection_claim ON aios.semantic_topology_projection USING btree (claim_id) WHERE (claim_id IS NOT NULL);


--
-- Name: idx_semantic_topology_scope; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_scope ON aios.semantic_topology_node USING btree (scope_key, node_type);


--
-- Name: idx_semantic_topology_source; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_source ON aios.semantic_topology_node USING btree (source_id, created_at DESC) WHERE (source_id IS NOT NULL);


--
-- Name: idx_semantic_topology_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_topology_world ON aios.semantic_topology_node USING btree (world_id, created_at DESC) WHERE (world_id IS NOT NULL);


--
-- Name: idx_semantic_validation_evidence; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_validation_evidence ON aios.semantic_validation_dependency USING btree (evidence_type, evidence_key);


--
-- Name: idx_semantic_validation_stale; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_validation_stale ON aios.semantic_validation_decision USING btree (status, stale_at) WHERE (status = 'stale'::text);


--
-- Name: idx_semantic_validation_subject; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_semantic_validation_subject ON aios.semantic_validation_decision USING btree (subject_type, subject_key);


--
-- Name: idx_session_created_at; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_session_created_at ON aios.session USING btree (created_at DESC);


--
-- Name: idx_session_source_session; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_session_source_session ON aios.session USING btree (source, source_session_id);


--
-- Name: idx_source_document_type; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_source_document_type ON aios.source_document USING btree (source_type);


--
-- Name: idx_source_identity_domain; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_source_identity_domain ON aios.source_identity USING btree (canonical_domain) WHERE (canonical_domain IS NOT NULL);


--
-- Name: idx_timeline_character_created; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_timeline_character_created ON aios.timeline USING btree (character_id, created_at DESC);


--
-- Name: idx_timeline_lookup; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_timeline_lookup ON aios.timeline USING btree (character_id, user_name, scope_key, created_at DESC);


--
-- Name: idx_timeline_source_identity; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_timeline_source_identity ON aios.timeline USING btree (source_id, created_at DESC) WHERE (source_id IS NOT NULL);


--
-- Name: idx_timeline_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_timeline_world ON aios.timeline USING btree (world_id);


--
-- Name: idx_world_anchor_node_id; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_anchor_node_id ON aios.world USING btree (anchor_node_id);


--
-- Name: idx_world_canon; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_canon ON aios.world USING btree (canon_of_world_id);


--
-- Name: idx_world_entity_relation_subject; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_entity_relation_subject ON aios.world_entity_relation USING btree (world_id, subject_entity_id, relation_type) WHERE (valid_to_node_id IS NULL);


--
-- Name: idx_world_entity_world_type; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_entity_world_type ON aios.world_entity USING btree (world_id, entity_type);


--
-- Name: idx_world_event_causal_coordinate; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_event_causal_coordinate ON aios.world_event USING btree (world_id, timeline_id, domain_id, actor_entity_id, occurred_at DESC) WHERE (domain_id IS NOT NULL);


--
-- Name: idx_world_event_exposure_instance; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_event_exposure_instance ON aios.world_event_exposure USING btree (instance_id, created_at DESC);


--
-- Name: idx_world_event_exposure_world_node; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_event_exposure_world_node ON aios.world_event_exposure USING btree (world_node_id, instance_id);


--
-- Name: idx_world_event_world_created; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_event_world_created ON aios.world_event USING btree (world_id, created_at DESC);


--
-- Name: idx_world_memory_state_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_memory_state_world ON aios.world_memory_state USING btree (world_id, stance, state_confidence DESC);


--
-- Name: idx_world_origin_character_id; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_origin_character_id ON aios.world USING btree (origin_character_id);


--
-- Name: idx_world_parent; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_parent ON aios.world USING btree (parent_world_id);


--
-- Name: idx_world_proposition_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_proposition_status ON aios.world_proposition_assertion USING btree (world_id, epistemic_status, source_kind);


--
-- Name: idx_world_root_world_id; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_root_world_id ON aios.world USING btree (root_world_id);


--
-- Name: idx_world_type; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_type ON aios.world USING btree (world_type);


--
-- Name: ix_pipeline_job_lease_expiry; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX ix_pipeline_job_lease_expiry ON aios.pipeline_job USING btree (lease_expires_at) WHERE (status = 'running'::text);


--
-- Name: ix_pipeline_job_running_partition; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX ix_pipeline_job_running_partition ON aios.pipeline_job USING btree (partition_key) WHERE ((status = 'running'::text) AND (partition_key IS NOT NULL));


--
-- Name: ix_pipeline_job_scheduler_lane; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX ix_pipeline_job_scheduler_lane ON aios.pipeline_job USING btree (resource_class, scheduling_lane, priority, created_at) WHERE (status = 'queued'::text);


--
-- Name: ix_pipeline_job_scheduler_ready; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX ix_pipeline_job_scheduler_ready ON aios.pipeline_job USING btree (resource_class, priority, created_at) WHERE (status = 'queued'::text);


--
-- Name: semantic_interpretation_claim_idx; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX semantic_interpretation_claim_idx ON aios.semantic_interpretation USING btree (claim_id);


--
-- Name: semantic_interpretation_role_child_idx; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX semantic_interpretation_role_child_idx ON aios.semantic_interpretation_role USING btree (child_frame_id) WHERE (child_frame_id IS NOT NULL);


--
-- Name: semantic_interpretation_role_entity_idx; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX semantic_interpretation_role_entity_idx ON aios.semantic_interpretation_role USING btree (entity_key) WHERE (entity_key IS NOT NULL);


--
-- Name: semantic_interpretation_type_idx; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX semantic_interpretation_type_idx ON aios.semantic_interpretation USING btree (semantic_type, standalone_semantic);


--
-- Name: semantic_neighbor_candidate_score_idx; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX semantic_neighbor_candidate_score_idx ON aios.semantic_neighbor_candidate USING btree (similarity DESC);


--
-- Name: semantic_vector_index_state_lookup_idx; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX semantic_vector_index_state_lookup_idx ON aios.semantic_vector_index_state USING btree (object_type, indexed_at);


--
-- Name: ux_pipeline_job_acquisition_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_job_acquisition_active ON aios.pipeline_job USING btree (job_type, ((payload ->> 'acquisition_id'::text))) WHERE ((status = ANY (ARRAY['queued'::text, 'running'::text])) AND (payload ? 'acquisition_id'::text));


--
-- Name: ux_pipeline_job_assertion_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_job_assertion_active ON aios.pipeline_job USING btree (job_type, ((payload ->> 'assertion_id'::text))) WHERE ((status = ANY (ARRAY['queued'::text, 'running'::text])) AND (payload ? 'assertion_id'::text));


--
-- Name: ux_pipeline_job_character_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_job_character_active ON aios.pipeline_job USING btree (job_type, ((payload ->> 'character_id'::text))) WHERE ((status = ANY (ARRAY['queued'::text, 'running'::text])) AND (payload ? 'character_id'::text));


--
-- Name: ux_pipeline_job_claim_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_job_claim_active ON aios.pipeline_job USING btree (job_type, ((payload ->> 'claim_id'::text))) WHERE ((status = ANY (ARRAY['queued'::text, 'running'::text])) AND (payload ? 'claim_id'::text));


--
-- Name: ux_pipeline_job_global_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_job_global_active ON aios.pipeline_job USING btree (job_type) WHERE ((status = ANY (ARRAY['queued'::text, 'running'::text])) AND (NOT (payload ? 'node_id'::text)) AND (NOT (payload ? 'section_id'::text)) AND (NOT (payload ? 'claim_id'::text)) AND (NOT (payload ? 'character_id'::text)) AND (NOT (payload ? 'world_id'::text)) AND (NOT (payload ? 'assertion_id'::text)) AND (NOT (payload ? 'acquisition_id'::text)));


--
-- Name: ux_pipeline_job_node_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_job_node_active ON aios.pipeline_job USING btree (job_type, ((payload ->> 'node_id'::text))) WHERE ((status = ANY (ARRAY['queued'::text, 'running'::text])) AND (payload ? 'node_id'::text));


--
-- Name: ux_pipeline_job_section_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_job_section_active ON aios.pipeline_job USING btree (job_type, ((payload ->> 'section_id'::text))) WHERE ((status = ANY (ARRAY['queued'::text, 'running'::text])) AND (payload ? 'section_id'::text));


--
-- Name: ux_pipeline_job_world_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_job_world_active ON aios.pipeline_job USING btree (job_type, ((payload ->> 'world_id'::text))) WHERE ((status = ANY (ARRAY['queued'::text, 'running'::text])) AND (payload ? 'world_id'::text));


--
-- Name: ux_pipeline_project_semantic_scope_active; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_pipeline_project_semantic_scope_active ON aios.pipeline_job USING btree (((payload ->> 'scope_key'::text))) WHERE ((job_type = 'project_semantic_scope'::text) AND (status = ANY (ARRAY['queued'::text, 'running'::text])));


--
-- Name: ux_proposition_conflict_pair; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_proposition_conflict_pair ON aios.proposition_conflict USING btree (LEAST(proposition_a_id, proposition_b_id), GREATEST(proposition_a_id, proposition_b_id), conflict_type);


--
-- Name: ux_rdf_promotion_claim_graph_predicate; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_rdf_promotion_claim_graph_predicate ON aios.rdf_promotion_log USING btree (claim_id, rdf_dataset, rdf_graph, rdf_predicate);


--
-- Name: ux_timeline_identity; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_timeline_identity ON aios.timeline USING btree (world_id, name, session_id, character_id, user_name, scope_key, source_id) NULLS NOT DISTINCT;


--
-- Name: ux_world_event_causal_candidate; Type: INDEX; Schema: aios; Owner: -
--

CREATE UNIQUE INDEX ux_world_event_causal_candidate ON aios.world_event USING btree (candidate_id) WHERE (candidate_id IS NOT NULL);


--
-- Name: proposition trg_assign_semantic_atom; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_assign_semantic_atom BEFORE INSERT OR UPDATE OF subject_norm, predicate_norm, object_norm, canonical_text ON aios.proposition FOR EACH ROW EXECUTE FUNCTION aios.assign_semantic_atom();


--
-- Name: semantic_evidence_admission trg_enforce_frame_interpretation_admission; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_enforce_frame_interpretation_admission BEFORE INSERT OR UPDATE ON aios.semantic_evidence_admission FOR EACH ROW EXECUTE FUNCTION aios.enforce_frame_interpretation_admission();


--
-- Name: world trg_ensure_world_objective_timeline; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_ensure_world_objective_timeline AFTER INSERT ON aios.world FOR EACH ROW EXECUTE FUNCTION aios.ensure_new_world_objective_timeline();


--
-- Name: timeline trg_link_character_timeline_to_world; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_link_character_timeline_to_world AFTER INSERT OR UPDATE OF world_id, character_id, meta ON aios.timeline FOR EACH ROW EXECUTE FUNCTION aios.link_character_timeline_to_world();


--
-- Name: character_belief_state trg_mark_belief_scope_dirty_insert_delete; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_mark_belief_scope_dirty_insert_delete AFTER INSERT OR DELETE ON aios.character_belief_state FOR EACH ROW EXECUTE FUNCTION aios.mark_belief_scope_dirty();


--
-- Name: character_belief_state trg_mark_belief_scope_dirty_update; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_mark_belief_scope_dirty_update AFTER UPDATE OF stance, positive_support, negative_support, preferred_proposition_id, resolved_through_node_id ON aios.character_belief_state FOR EACH ROW WHEN (((old.stance IS DISTINCT FROM new.stance) OR (old.positive_support IS DISTINCT FROM new.positive_support) OR (old.negative_support IS DISTINCT FROM new.negative_support) OR (old.preferred_proposition_id IS DISTINCT FROM new.preferred_proposition_id) OR (old.resolved_through_node_id IS DISTINCT FROM new.resolved_through_node_id))) EXECUTE FUNCTION aios.mark_belief_scope_dirty();


--
-- Name: character_belief_state trg_mark_character_belief_rdf_dirty; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_mark_character_belief_rdf_dirty AFTER INSERT OR DELETE OR UPDATE OF stance, positive_support, negative_support, belief_confidence, preferred_proposition_id ON aios.character_belief_state FOR EACH ROW EXECUTE FUNCTION aios.mark_character_belief_rdf_dirty();


--
-- Name: observation trg_project_observation_to_runtime_perceivers; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_project_observation_to_runtime_perceivers AFTER INSERT OR UPDATE OF proposition_id, timeline_id, dag_node_id ON aios.observation FOR EACH ROW EXECUTE FUNCTION aios.project_observation_to_runtime_perceivers();


--
-- Name: semantic_neighbor_candidate trg_reconsider_unresolved_topology_from_neighbor; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_reconsider_unresolved_topology_from_neighbor AFTER INSERT OR UPDATE OF similarity, status ON aios.semantic_neighbor_candidate FOR EACH ROW EXECUTE FUNCTION aios.reconsider_unresolved_topology_from_neighbor();


--
-- Name: claim_context_resolution trg_refresh_admission_from_context; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_admission_from_context AFTER INSERT OR UPDATE OF epistemic_scope, confidence, resolver_version ON aios.claim_context_resolution FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_for_claim();


--
-- Name: claim_semantic_frame trg_refresh_admission_from_frame; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_admission_from_frame AFTER INSERT OR UPDATE OF resolved_subject, resolved_object, predicate_canonical, resolution_status, discourse_mode, frame_confidence, referent_confidence ON aios.claim_semantic_frame FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_for_claim();


--
-- Name: semantic_topology_node trg_refresh_belief_from_acquisition_topology; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_belief_from_acquisition_topology AFTER INSERT OR UPDATE OF acquisition_id ON aios.semantic_topology_node FOR EACH ROW WHEN (((new.node_type = 'EPISTEMIC_TRANSITION'::text) AND (new.acquisition_id IS NOT NULL))) EXECUTE FUNCTION aios.refresh_belief_from_acquisition_topology();


--
-- Name: semantic_evidence_admission trg_refresh_belief_from_admission; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_belief_from_admission AFTER INSERT OR DELETE ON aios.semantic_evidence_admission FOR EACH ROW EXECUTE FUNCTION aios.refresh_belief_from_admission();


--
-- Name: semantic_evidence_admission trg_refresh_belief_from_admission_update; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_belief_from_admission_update AFTER UPDATE OF status, confidence ON aios.semantic_evidence_admission FOR EACH ROW EXECUTE FUNCTION aios.refresh_belief_from_admission();


--
-- Name: character_proposition_knowledge trg_refresh_belief_from_character_evidence; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_belief_from_character_evidence AFTER INSERT OR DELETE OR UPDATE ON aios.character_proposition_knowledge FOR EACH ROW EXECUTE FUNCTION aios.refresh_belief_from_character_evidence();


--
-- Name: ingest_event trg_refresh_belief_from_ingest_supersession; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_belief_from_ingest_supersession AFTER UPDATE OF superseded_at ON aios.ingest_event FOR EACH ROW EXECUTE FUNCTION aios.refresh_belief_from_ingest_supersession();


--
-- Name: semantic_topology_projection trg_refresh_message_enrichment_cursor; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_message_enrichment_cursor AFTER INSERT OR UPDATE OF projected_at ON aios.semantic_topology_projection FOR EACH ROW WHEN ((new.projected_at IS NOT NULL)) EXECUTE FUNCTION aios.refresh_message_enrichment_cursor();


--
-- Name: semantic_topology_projection trg_refresh_message_enrichment_projection; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_message_enrichment_projection AFTER INSERT OR UPDATE OF projected_at ON aios.semantic_topology_projection FOR EACH ROW EXECUTE FUNCTION aios.refresh_message_enrichment_from_projection();


--
-- Name: document_section trg_refresh_message_enrichment_section; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_message_enrichment_section AFTER UPDATE OF claims_extracted_at ON aios.document_section FOR EACH ROW EXECUTE FUNCTION aios.refresh_message_enrichment_from_section();


--
-- Name: knowledge_acquisition_event trg_refresh_semantic_admission_from_acquisition; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_semantic_admission_from_acquisition AFTER INSERT OR UPDATE OF claim_id, proposition_id, instance_id, confidence, meta ON aios.knowledge_acquisition_event FOR EACH ROW EXECUTE FUNCTION aios.refresh_admission_from_acquisition();


--
-- Name: world_proposition_assertion trg_refresh_world_memory_from_assertion; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_refresh_world_memory_from_assertion AFTER INSERT OR DELETE OR UPDATE ON aios.world_proposition_assertion FOR EACH ROW EXECUTE FUNCTION aios.refresh_world_memory_from_assertion();


--
-- Name: retention_event trg_retention_event_append_only; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_retention_event_append_only BEFORE DELETE OR UPDATE ON aios.retention_event FOR EACH ROW EXECUTE FUNCTION aios.reject_retention_event_mutation();


--
-- Name: observation trg_route_normalized_memory_evidence; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_route_normalized_memory_evidence AFTER INSERT OR UPDATE OF proposition_id ON aios.observation FOR EACH ROW EXECUTE FUNCTION aios.route_normalized_memory_evidence();


--
-- Name: claim_semantic_frame trg_semantic_frame_policy_v3; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_semantic_frame_policy_v3 BEFORE UPDATE OF resolved_subject, resolved_object, resolution_status, referent_confidence, frame_confidence, predicate_canonical ON aios.claim_semantic_frame FOR EACH ROW EXECUTE FUNCTION aios.semantic_frame_policy_v3();


--
-- Name: character_alias trg_semantic_validation_character_alias; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_semantic_validation_character_alias AFTER INSERT OR UPDATE OF alias, character_id ON aios.character_alias FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_character_alias_trigger();


--
-- Name: character_identity trg_semantic_validation_character_identity; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_semantic_validation_character_identity AFTER INSERT OR UPDATE OF character_id, display_name, canonical_name ON aios.character_identity FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_character_identity_trigger();


--
-- Name: claim_context_resolution trg_semantic_validation_claim_context; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_semantic_validation_claim_context AFTER INSERT OR UPDATE ON aios.claim_context_resolution FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_context_trigger();


--
-- Name: proposition_conflict trg_semantic_validation_conflict; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_semantic_validation_conflict AFTER INSERT OR UPDATE OF conflict_type, strength ON aios.proposition_conflict FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_conflict_trigger();


--
-- Name: semantic_validation_decision trg_semantic_validation_decision; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_semantic_validation_decision AFTER INSERT OR UPDATE OF selected_value, status ON aios.semantic_validation_decision FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_decision_trigger();


--
-- Name: semantic_neighbor_candidate trg_semantic_validation_neighbor; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_semantic_validation_neighbor AFTER INSERT OR UPDATE OF similarity, status ON aios.semantic_neighbor_candidate FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_neighbor_trigger();


--
-- Name: world_proposition_assertion trg_semantic_validation_world_assertion; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_semantic_validation_world_assertion AFTER INSERT OR UPDATE OF epistemic_status, confidence, superseded_by_assertion_id ON aios.world_proposition_assertion FOR EACH ROW EXECUTE FUNCTION aios.semantic_validation_world_assertion_trigger();


--
-- Name: proposition_conflict trg_validate_proposition_conflict_identity; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_validate_proposition_conflict_identity BEFORE INSERT OR UPDATE OF conflict_type, proposition_a_id, proposition_b_id ON aios.proposition_conflict FOR EACH ROW EXECUTE FUNCTION aios.validate_proposition_conflict_identity();


--
-- Name: world_event_exposure trg_validate_world_event_exposure; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_validate_world_event_exposure BEFORE INSERT OR UPDATE ON aios.world_event_exposure FOR EACH ROW EXECUTE FUNCTION aios.validate_world_event_exposure();


--
-- Name: knowledge_acquisition_event trg_zz_auto_evaluate_acquisition_retention; Type: TRIGGER; Schema: aios; Owner: -
--

CREATE TRIGGER trg_zz_auto_evaluate_acquisition_retention AFTER INSERT OR UPDATE OF proposition_id, claim_id, instance_id, dag_node_id ON aios.knowledge_acquisition_event FOR EACH ROW EXECUTE FUNCTION aios.auto_evaluate_acquisition_retention();


--
-- Name: causal_admission causal_admission_candidate_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_admission
    ADD CONSTRAINT causal_admission_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES aios.causal_candidate(candidate_id) ON DELETE CASCADE;


--
-- Name: causal_candidate causal_candidate_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE SET NULL;


--
-- Name: causal_candidate causal_candidate_dag_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_dag_node_id_fkey FOREIGN KEY (dag_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: causal_candidate causal_candidate_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES aios.world_entity(entity_id) ON DELETE CASCADE;


--
-- Name: causal_candidate causal_candidate_frame_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE SET NULL;


--
-- Name: causal_candidate causal_candidate_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL;


--
-- Name: causal_candidate causal_candidate_target_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_target_entity_id_fkey FOREIGN KEY (target_entity_id) REFERENCES aios.world_entity(entity_id) ON DELETE SET NULL;


--
-- Name: causal_candidate causal_candidate_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: causal_candidate causal_candidate_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_candidate
    ADD CONSTRAINT causal_candidate_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: causal_state causal_state_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_state
    ADD CONSTRAINT causal_state_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES aios.world_entity(entity_id) ON DELETE CASCADE;


--
-- Name: causal_state causal_state_last_event_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_state
    ADD CONSTRAINT causal_state_last_event_fkey FOREIGN KEY (last_event_id) REFERENCES aios.world_event(world_event_id) ON DELETE SET NULL;


--
-- Name: causal_state causal_state_last_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_state
    ADD CONSTRAINT causal_state_last_node_id_fkey FOREIGN KEY (last_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: causal_state causal_state_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_state
    ADD CONSTRAINT causal_state_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: causal_state causal_state_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.causal_state
    ADD CONSTRAINT causal_state_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: character_alias character_alias_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_alias
    ADD CONSTRAINT character_alias_character_id_fkey FOREIGN KEY (character_id) REFERENCES aios.character_identity(character_id) ON DELETE CASCADE;


--
-- Name: character_belief_state character_belief_state_atom_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_belief_state
    ADD CONSTRAINT character_belief_state_atom_id_fkey FOREIGN KEY (atom_id) REFERENCES aios.semantic_atom(atom_id) ON DELETE CASCADE;


--
-- Name: character_belief_state character_belief_state_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_belief_state
    ADD CONSTRAINT character_belief_state_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: character_belief_state character_belief_state_preferred_evidence_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_belief_state
    ADD CONSTRAINT character_belief_state_preferred_evidence_instance_id_fkey FOREIGN KEY (preferred_evidence_instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL;


--
-- Name: character_belief_state character_belief_state_preferred_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_belief_state
    ADD CONSTRAINT character_belief_state_preferred_proposition_id_fkey FOREIGN KEY (preferred_proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL;


--
-- Name: character_belief_state character_belief_state_resolved_through_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_belief_state
    ADD CONSTRAINT character_belief_state_resolved_through_node_id_fkey FOREIGN KEY (resolved_through_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: character_epistemic_profile character_epistemic_profile_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_epistemic_profile
    ADD CONSTRAINT character_epistemic_profile_character_id_fkey FOREIGN KEY (character_id) REFERENCES aios.character_identity(character_id) ON DELETE CASCADE;


--
-- Name: character_hud_profile character_hud_profile_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_profile
    ADD CONSTRAINT character_hud_profile_character_id_fkey FOREIGN KEY (character_id) REFERENCES aios.character_identity(character_id) ON DELETE CASCADE;


--
-- Name: character_hud_profile character_hud_profile_profile_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_profile
    ADD CONSTRAINT character_hud_profile_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES aios.hud_profile(profile_id) ON DELETE RESTRICT;


--
-- Name: character_hud_readiness character_hud_readiness_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_readiness
    ADD CONSTRAINT character_hud_readiness_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: character_hud_readiness character_hud_readiness_prepared_source_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_readiness
    ADD CONSTRAINT character_hud_readiness_prepared_source_node_id_fkey FOREIGN KEY (prepared_source_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: character_hud_readiness character_hud_readiness_retrieval_ready_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_readiness
    ADD CONSTRAINT character_hud_readiness_retrieval_ready_node_id_fkey FOREIGN KEY (retrieval_ready_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: character_hud_readiness character_hud_readiness_source_head_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_readiness
    ADD CONSTRAINT character_hud_readiness_source_head_node_id_fkey FOREIGN KEY (source_head_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: character_hud_readiness character_hud_readiness_source_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_hud_readiness
    ADD CONSTRAINT character_hud_readiness_source_timeline_id_fkey FOREIGN KEY (source_timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL;


--
-- Name: character_identity character_identity_home_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_identity
    ADD CONSTRAINT character_identity_home_world_id_fkey FOREIGN KEY (home_world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: character_instance character_instance_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_character_id_fkey FOREIGN KEY (character_id) REFERENCES aios.character_identity(character_id) ON DELETE CASCADE;


--
-- Name: character_instance character_instance_current_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_current_world_id_fkey FOREIGN KEY (current_world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: character_instance character_instance_forked_from_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_forked_from_node_id_fkey FOREIGN KEY (forked_from_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: character_instance character_instance_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES aios.user_identity(user_id) ON DELETE SET NULL;


--
-- Name: character_instance character_instance_parent_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_parent_instance_id_fkey FOREIGN KEY (parent_instance_id) REFERENCES aios.character_instance(instance_id);


--
-- Name: character_instance character_instance_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: character_inventory character_inventory_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_inventory
    ADD CONSTRAINT character_inventory_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: character_inventory character_inventory_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_inventory
    ADD CONSTRAINT character_inventory_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: character_knowledge character_knowledge_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_knowledge
    ADD CONSTRAINT character_knowledge_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: character_knowledge character_knowledge_first_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_knowledge
    ADD CONSTRAINT character_knowledge_first_node_id_fkey FOREIGN KEY (first_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: character_knowledge character_knowledge_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_knowledge
    ADD CONSTRAINT character_knowledge_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: character_knowledge character_knowledge_last_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_knowledge
    ADD CONSTRAINT character_knowledge_last_node_id_fkey FOREIGN KEY (last_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: character_knowledge character_knowledge_source_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_knowledge
    ADD CONSTRAINT character_knowledge_source_entity_id_fkey FOREIGN KEY (source_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: character_proposition_knowledge character_proposition_knowledge_first_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_proposition_knowledge
    ADD CONSTRAINT character_proposition_knowledge_first_node_id_fkey FOREIGN KEY (first_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: character_proposition_knowledge character_proposition_knowledge_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_proposition_knowledge
    ADD CONSTRAINT character_proposition_knowledge_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: character_proposition_knowledge character_proposition_knowledge_last_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_proposition_knowledge
    ADD CONSTRAINT character_proposition_knowledge_last_node_id_fkey FOREIGN KEY (last_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: character_proposition_knowledge character_proposition_knowledge_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_proposition_knowledge
    ADD CONSTRAINT character_proposition_knowledge_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: character_proposition_knowledge character_proposition_knowledge_source_entity_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_proposition_knowledge
    ADD CONSTRAINT character_proposition_knowledge_source_entity_fkey FOREIGN KEY (source_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: character_relationship character_relationship_observer_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_relationship
    ADD CONSTRAINT character_relationship_observer_instance_id_fkey FOREIGN KEY (observer_instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: character_relationship character_relationship_target_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_relationship
    ADD CONSTRAINT character_relationship_target_entity_id_fkey FOREIGN KEY (target_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: character_runtime_state character_runtime_state_head_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_runtime_state
    ADD CONSTRAINT character_runtime_state_head_node_id_fkey FOREIGN KEY (head_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: character_runtime_state character_runtime_state_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_runtime_state
    ADD CONSTRAINT character_runtime_state_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: character_runtime_state character_runtime_state_location_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_runtime_state
    ADD CONSTRAINT character_runtime_state_location_entity_id_fkey FOREIGN KEY (location_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: character_runtime_state character_runtime_state_source_head_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_runtime_state
    ADD CONSTRAINT character_runtime_state_source_head_node_id_fkey FOREIGN KEY (source_head_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: character_runtime_state character_runtime_state_source_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_runtime_state
    ADD CONSTRAINT character_runtime_state_source_timeline_id_fkey FOREIGN KEY (source_timeline_id) REFERENCES aios.timeline(timeline_id);


--
-- Name: character_runtime_state character_runtime_state_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_runtime_state
    ADD CONSTRAINT character_runtime_state_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id);


--
-- Name: character_runtime_state character_runtime_state_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_runtime_state
    ADD CONSTRAINT character_runtime_state_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id);


--
-- Name: character_world_timeline_link character_world_timeline_link_character_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_world_timeline_link
    ADD CONSTRAINT character_world_timeline_link_character_timeline_id_fkey FOREIGN KEY (character_timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: character_world_timeline_link character_world_timeline_link_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_world_timeline_link
    ADD CONSTRAINT character_world_timeline_link_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: character_world_timeline_link character_world_timeline_link_world_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_world_timeline_link
    ADD CONSTRAINT character_world_timeline_link_world_timeline_id_fkey FOREIGN KEY (world_timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: claim_candidate claim_candidate_sentence_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_candidate
    ADD CONSTRAINT claim_candidate_sentence_id_fkey FOREIGN KEY (sentence_id) REFERENCES aios.extracted_sentence(sentence_id) ON DELETE CASCADE;


--
-- Name: claim_context_resolution claim_context_resolution_character_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_context_resolution
    ADD CONSTRAINT claim_context_resolution_character_instance_id_fkey FOREIGN KEY (character_instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL;


--
-- Name: claim_context_resolution claim_context_resolution_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_context_resolution
    ADD CONSTRAINT claim_context_resolution_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: claim_context_resolution claim_context_resolution_dag_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_context_resolution
    ADD CONSTRAINT claim_context_resolution_dag_node_id_fkey FOREIGN KEY (dag_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: claim_context_resolution claim_context_resolution_source_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_context_resolution
    ADD CONSTRAINT claim_context_resolution_source_id_fkey FOREIGN KEY (source_id) REFERENCES aios.source_identity(source_id) ON DELETE SET NULL;


--
-- Name: claim_context_resolution claim_context_resolution_target_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_context_resolution
    ADD CONSTRAINT claim_context_resolution_target_world_id_fkey FOREIGN KEY (target_world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: claim_context_resolution claim_context_resolution_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_context_resolution
    ADD CONSTRAINT claim_context_resolution_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL;


--
-- Name: claim_context_resolution claim_context_resolution_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_context_resolution
    ADD CONSTRAINT claim_context_resolution_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: claim_contradiction_candidate claim_contradiction_candidate_claim_a_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_contradiction_candidate
    ADD CONSTRAINT claim_contradiction_candidate_claim_a_id_fkey FOREIGN KEY (claim_a_id) REFERENCES aios.claim_candidate(claim_id);


--
-- Name: claim_contradiction_candidate claim_contradiction_candidate_claim_b_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_contradiction_candidate
    ADD CONSTRAINT claim_contradiction_candidate_claim_b_id_fkey FOREIGN KEY (claim_b_id) REFERENCES aios.claim_candidate(claim_id);


--
-- Name: claim_provenance claim_provenance_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_provenance
    ADD CONSTRAINT claim_provenance_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: claim_provenance claim_provenance_document_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_provenance
    ADD CONSTRAINT claim_provenance_document_id_fkey FOREIGN KEY (document_id) REFERENCES aios.source_document(document_id) ON DELETE CASCADE;


--
-- Name: claim_semantic_frame claim_semantic_frame_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_semantic_frame
    ADD CONSTRAINT claim_semantic_frame_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: claim_semantic_frame claim_semantic_frame_object_frame_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_semantic_frame
    ADD CONSTRAINT claim_semantic_frame_object_frame_id_fkey FOREIGN KEY (object_frame_id) REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE SET NULL;


--
-- Name: claim_semantic_frame claim_semantic_frame_parent_frame_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_semantic_frame
    ADD CONSTRAINT claim_semantic_frame_parent_frame_id_fkey FOREIGN KEY (parent_frame_id) REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE SET NULL;


--
-- Name: claim_semantic_frame_projection claim_semantic_frame_projection_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_semantic_frame_projection
    ADD CONSTRAINT claim_semantic_frame_projection_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: claim_semantic_frame_projection claim_semantic_frame_projection_primary_frame_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_semantic_frame_projection
    ADD CONSTRAINT claim_semantic_frame_projection_primary_frame_id_fkey FOREIGN KEY (primary_frame_id) REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE SET NULL;


--
-- Name: claim_similarity_edge claim_similarity_edge_claim_a_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_similarity_edge
    ADD CONSTRAINT claim_similarity_edge_claim_a_id_fkey FOREIGN KEY (claim_a_id) REFERENCES aios.claim_candidate(claim_id);


--
-- Name: claim_similarity_edge claim_similarity_edge_claim_b_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_similarity_edge
    ADD CONSTRAINT claim_similarity_edge_claim_b_id_fkey FOREIGN KEY (claim_b_id) REFERENCES aios.claim_candidate(claim_id);


--
-- Name: claim_world_assignment claim_world_assignment_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_world_assignment
    ADD CONSTRAINT claim_world_assignment_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: dag_edge dag_edge_child_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.dag_edge
    ADD CONSTRAINT dag_edge_child_node_id_fkey FOREIGN KEY (child_node_id) REFERENCES aios.dag_node(node_id) ON DELETE CASCADE;


--
-- Name: dag_edge dag_edge_parent_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.dag_edge
    ADD CONSTRAINT dag_edge_parent_node_id_fkey FOREIGN KEY (parent_node_id) REFERENCES aios.dag_node(node_id) ON DELETE CASCADE;


--
-- Name: dag_edge dag_edge_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.dag_edge
    ADD CONSTRAINT dag_edge_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: dag_node dag_node_event_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.dag_node
    ADD CONSTRAINT dag_node_event_id_fkey FOREIGN KEY (event_id) REFERENCES aios.ingest_event(event_id) ON DELETE CASCADE;


--
-- Name: dag_node dag_node_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.dag_node
    ADD CONSTRAINT dag_node_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: document_metadata_observation document_metadata_observation_document_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_metadata_observation
    ADD CONSTRAINT document_metadata_observation_document_id_fkey FOREIGN KEY (document_id) REFERENCES aios.source_document(document_id) ON DELETE CASCADE;


--
-- Name: document_metadata_observation document_metadata_observation_source_unit_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_metadata_observation
    ADD CONSTRAINT document_metadata_observation_source_unit_id_fkey FOREIGN KEY (source_unit_id) REFERENCES aios.document_unit(unit_id);


--
-- Name: document_section document_section_document_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_section
    ADD CONSTRAINT document_section_document_id_fkey FOREIGN KEY (document_id) REFERENCES aios.source_document(document_id) ON DELETE CASCADE;


--
-- Name: document_section document_section_node_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_section
    ADD CONSTRAINT document_section_node_fkey FOREIGN KEY (node_id) REFERENCES aios.dag_node(node_id) ON DELETE CASCADE;


--
-- Name: document_unit document_unit_document_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_unit
    ADD CONSTRAINT document_unit_document_id_fkey FOREIGN KEY (document_id) REFERENCES aios.source_document(document_id) ON DELETE CASCADE;


--
-- Name: document_unit document_unit_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_unit
    ADD CONSTRAINT document_unit_node_id_fkey FOREIGN KEY (node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: document_unit document_unit_parent_unit_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.document_unit
    ADD CONSTRAINT document_unit_parent_unit_id_fkey FOREIGN KEY (parent_unit_id) REFERENCES aios.document_unit(unit_id) ON DELETE CASCADE;


--
-- Name: entity_controller entity_controller_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.entity_controller
    ADD CONSTRAINT entity_controller_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES aios.world_entity(entity_id) ON DELETE CASCADE;


--
-- Name: extracted_sentence extracted_sentence_section_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.extracted_sentence
    ADD CONSTRAINT extracted_sentence_section_id_fkey FOREIGN KEY (section_id) REFERENCES aios.document_section(section_id) ON DELETE CASCADE;


--
-- Name: ingest_event ingest_event_session_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.ingest_event
    ADD CONSTRAINT ingest_event_session_id_fkey FOREIGN KEY (session_id) REFERENCES aios.session(session_id) ON DELETE SET NULL;


--
-- Name: ingest_event ingest_event_source_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.ingest_event
    ADD CONSTRAINT ingest_event_source_id_fkey FOREIGN KEY (source_id) REFERENCES aios.source_identity(source_id) ON DELETE SET NULL;


--
-- Name: ingest_event ingest_event_superseded_by_event_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.ingest_event
    ADD CONSTRAINT ingest_event_superseded_by_event_id_fkey FOREIGN KEY (superseded_by_event_id) REFERENCES aios.ingest_event(event_id) ON DELETE SET NULL;


--
-- Name: ingest_event ingest_event_target_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.ingest_event
    ADD CONSTRAINT ingest_event_target_world_id_fkey FOREIGN KEY (target_world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: knowledge_acquisition_event knowledge_acquisition_event_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.knowledge_acquisition_event
    ADD CONSTRAINT knowledge_acquisition_event_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: knowledge_acquisition_event knowledge_acquisition_event_dag_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.knowledge_acquisition_event
    ADD CONSTRAINT knowledge_acquisition_event_dag_node_id_fkey FOREIGN KEY (dag_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: knowledge_acquisition_event knowledge_acquisition_event_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.knowledge_acquisition_event
    ADD CONSTRAINT knowledge_acquisition_event_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: knowledge_acquisition_event knowledge_acquisition_event_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.knowledge_acquisition_event
    ADD CONSTRAINT knowledge_acquisition_event_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: knowledge_acquisition_event knowledge_acquisition_source_entity_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.knowledge_acquisition_event
    ADD CONSTRAINT knowledge_acquisition_source_entity_fkey FOREIGN KEY (source_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: memory_item memory_item_derived_from_event_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_item
    ADD CONSTRAINT memory_item_derived_from_event_id_fkey FOREIGN KEY (derived_from_event_id) REFERENCES aios.ingest_event(event_id) ON DELETE SET NULL;


--
-- Name: memory_item memory_item_derived_from_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_item
    ADD CONSTRAINT memory_item_derived_from_node_id_fkey FOREIGN KEY (derived_from_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: memory_item memory_item_thread_root_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_item
    ADD CONSTRAINT memory_item_thread_root_node_id_fkey FOREIGN KEY (thread_root_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: memory_item memory_item_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_item
    ADD CONSTRAINT memory_item_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: memory_reconciliation_receipt memory_reconciliation_receipt_assertion_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_reconciliation_receipt
    ADD CONSTRAINT memory_reconciliation_receipt_assertion_id_fkey FOREIGN KEY (assertion_id) REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE CASCADE;


--
-- Name: memory_reconciliation_receipt memory_reconciliation_receipt_atom_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_reconciliation_receipt
    ADD CONSTRAINT memory_reconciliation_receipt_atom_id_fkey FOREIGN KEY (atom_id) REFERENCES aios.semantic_atom(atom_id) ON DELETE CASCADE;


--
-- Name: memory_reconciliation_receipt memory_reconciliation_receipt_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_reconciliation_receipt
    ADD CONSTRAINT memory_reconciliation_receipt_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: memory_reconciliation_receipt memory_reconciliation_receipt_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_reconciliation_receipt
    ADD CONSTRAINT memory_reconciliation_receipt_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: message_cognitive_commit message_cognitive_commit_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.message_cognitive_commit
    ADD CONSTRAINT message_cognitive_commit_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: message_cognitive_commit message_cognitive_commit_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.message_cognitive_commit
    ADD CONSTRAINT message_cognitive_commit_node_id_fkey FOREIGN KEY (node_id) REFERENCES aios.dag_node(node_id) ON DELETE CASCADE;


--
-- Name: message_cognitive_unit message_cognitive_unit_commit_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.message_cognitive_unit
    ADD CONSTRAINT message_cognitive_unit_commit_id_fkey FOREIGN KEY (commit_id) REFERENCES aios.message_cognitive_commit(commit_id) ON DELETE CASCADE;


--
-- Name: message_cognitive_unit message_cognitive_unit_supersedes_unit_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.message_cognitive_unit
    ADD CONSTRAINT message_cognitive_unit_supersedes_unit_id_fkey FOREIGN KEY (supersedes_unit_id) REFERENCES aios.message_cognitive_unit(unit_id);


--
-- Name: narrative_membership narrative_membership_narrative_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.narrative_membership
    ADD CONSTRAINT narrative_membership_narrative_id_fkey FOREIGN KEY (narrative_id) REFERENCES aios.narrative_cluster(narrative_id) ON DELETE CASCADE;


--
-- Name: narrative_membership narrative_membership_observation_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.narrative_membership
    ADD CONSTRAINT narrative_membership_observation_id_fkey FOREIGN KEY (observation_id) REFERENCES aios.observation(observation_id) ON DELETE CASCADE;


--
-- Name: narrative_source_affinity narrative_source_affinity_narrative_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.narrative_source_affinity
    ADD CONSTRAINT narrative_source_affinity_narrative_id_fkey FOREIGN KEY (narrative_id) REFERENCES aios.narrative_cluster(narrative_id) ON DELETE CASCADE;


--
-- Name: observation observation_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation
    ADD CONSTRAINT observation_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: observation observation_dag_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation
    ADD CONSTRAINT observation_dag_node_id_fkey FOREIGN KEY (dag_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: observation observation_document_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation
    ADD CONSTRAINT observation_document_id_fkey FOREIGN KEY (document_id) REFERENCES aios.source_document(document_id);


--
-- Name: observation_proposition observation_proposition_frame_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation_proposition
    ADD CONSTRAINT observation_proposition_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE CASCADE;


--
-- Name: observation observation_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation
    ADD CONSTRAINT observation_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: observation_proposition observation_proposition_observation_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation_proposition
    ADD CONSTRAINT observation_proposition_observation_id_fkey FOREIGN KEY (observation_id) REFERENCES aios.observation(observation_id) ON DELETE CASCADE;


--
-- Name: observation_proposition observation_proposition_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation_proposition
    ADD CONSTRAINT observation_proposition_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: observation observation_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.observation
    ADD CONSTRAINT observation_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id);


--
-- Name: proposition proposition_atom_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition
    ADD CONSTRAINT proposition_atom_id_fkey FOREIGN KEY (atom_id) REFERENCES aios.semantic_atom(atom_id) ON DELETE RESTRICT;


--
-- Name: proposition_conflict proposition_conflict_proposition_a_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition_conflict
    ADD CONSTRAINT proposition_conflict_proposition_a_id_fkey FOREIGN KEY (proposition_a_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: proposition_conflict proposition_conflict_proposition_b_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition_conflict
    ADD CONSTRAINT proposition_conflict_proposition_b_id_fkey FOREIGN KEY (proposition_b_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: proposition_evidence proposition_evidence_observation_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition_evidence
    ADD CONSTRAINT proposition_evidence_observation_id_fkey FOREIGN KEY (observation_id) REFERENCES aios.observation(observation_id) ON DELETE CASCADE;


--
-- Name: proposition_evidence proposition_evidence_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.proposition_evidence
    ADD CONSTRAINT proposition_evidence_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: rdf_promotion_log rdf_promotion_log_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.rdf_promotion_log
    ADD CONSTRAINT rdf_promotion_log_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id);


--
-- Name: retention_event retention_event_related_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.retention_event
    ADD CONSTRAINT retention_event_related_node_id_fkey FOREIGN KEY (related_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: semantic_anchor_edge semantic_anchor_edge_acquisition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_acquisition_id_fkey FOREIGN KEY (acquisition_id) REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE;


--
-- Name: semantic_anchor_edge semantic_anchor_edge_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_character_id_fkey FOREIGN KEY (character_id) REFERENCES aios.character_identity(character_id) ON DELETE CASCADE;


--
-- Name: semantic_anchor_edge semantic_anchor_edge_character_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_character_instance_id_fkey FOREIGN KEY (character_instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: semantic_anchor_edge semantic_anchor_edge_dag_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_dag_node_id_fkey FOREIGN KEY (dag_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: semantic_anchor_edge semantic_anchor_edge_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_anchor_edge semantic_anchor_edge_source_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_source_node_id_fkey FOREIGN KEY (source_node_id) REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE CASCADE;


--
-- Name: semantic_anchor_edge semantic_anchor_edge_target_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_target_node_id_fkey FOREIGN KEY (target_node_id) REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE CASCADE;


--
-- Name: semantic_anchor_edge semantic_anchor_edge_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_anchor_edge
    ADD CONSTRAINT semantic_anchor_edge_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: semantic_boundary_classification semantic_boundary_classification_cluster_a_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_boundary_classification
    ADD CONSTRAINT semantic_boundary_classification_cluster_a_id_fkey FOREIGN KEY (cluster_a_id) REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE;


--
-- Name: semantic_boundary_classification semantic_boundary_classification_cluster_b_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_boundary_classification
    ADD CONSTRAINT semantic_boundary_classification_cluster_b_id_fkey FOREIGN KEY (cluster_b_id) REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE;


--
-- Name: semantic_boundary_classification semantic_boundary_classification_run_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_boundary_classification
    ADD CONSTRAINT semantic_boundary_classification_run_id_fkey FOREIGN KEY (run_id) REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE;


--
-- Name: semantic_branch_candidate semantic_branch_candidate_boundary_classification_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_boundary_classification_id_fkey FOREIGN KEY (boundary_classification_id) REFERENCES aios.semantic_boundary_classification(classification_id) ON DELETE CASCADE;


--
-- Name: semantic_branch_candidate semantic_branch_candidate_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_character_id_fkey FOREIGN KEY (character_id) REFERENCES aios.character_identity(character_id) ON DELETE CASCADE;


--
-- Name: semantic_branch_candidate semantic_branch_candidate_character_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_character_instance_id_fkey FOREIGN KEY (character_instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL;


--
-- Name: semantic_branch_candidate semantic_branch_candidate_cluster_a_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_cluster_a_id_fkey FOREIGN KEY (cluster_a_id) REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE;


--
-- Name: semantic_branch_candidate semantic_branch_candidate_cluster_b_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_cluster_b_id_fkey FOREIGN KEY (cluster_b_id) REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE;


--
-- Name: semantic_branch_candidate semantic_branch_candidate_run_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_run_id_fkey FOREIGN KEY (run_id) REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE;


--
-- Name: semantic_branch_candidate semantic_branch_candidate_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL;


--
-- Name: semantic_branch_candidate semantic_branch_candidate_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_branch_candidate
    ADD CONSTRAINT semantic_branch_candidate_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: semantic_cluster_boundary semantic_cluster_boundary_cluster_a_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_boundary
    ADD CONSTRAINT semantic_cluster_boundary_cluster_a_id_fkey FOREIGN KEY (cluster_a_id) REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE;


--
-- Name: semantic_cluster_boundary semantic_cluster_boundary_cluster_b_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_boundary
    ADD CONSTRAINT semantic_cluster_boundary_cluster_b_id_fkey FOREIGN KEY (cluster_b_id) REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE;


--
-- Name: semantic_cluster_boundary semantic_cluster_boundary_run_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_boundary
    ADD CONSTRAINT semantic_cluster_boundary_run_id_fkey FOREIGN KEY (run_id) REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE;


--
-- Name: semantic_cluster_candidate semantic_cluster_candidate_run_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_candidate
    ADD CONSTRAINT semantic_cluster_candidate_run_id_fkey FOREIGN KEY (run_id) REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE;


--
-- Name: semantic_cluster_classification semantic_cluster_classification_cluster_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_classification
    ADD CONSTRAINT semantic_cluster_classification_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE;


--
-- Name: semantic_cluster_classification semantic_cluster_classification_run_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_classification
    ADD CONSTRAINT semantic_cluster_classification_run_id_fkey FOREIGN KEY (run_id) REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE;


--
-- Name: semantic_cluster_membership semantic_cluster_membership_cluster_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_membership
    ADD CONSTRAINT semantic_cluster_membership_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE;


--
-- Name: semantic_cluster_membership semantic_cluster_membership_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_membership
    ADD CONSTRAINT semantic_cluster_membership_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_cluster_membership semantic_cluster_membership_strongest_neighbor_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_cluster_membership
    ADD CONSTRAINT semantic_cluster_membership_strongest_neighbor_id_fkey FOREIGN KEY (strongest_neighbor_id) REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL;


--
-- Name: semantic_evidence_admission semantic_evidence_admission_acquisition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_evidence_admission
    ADD CONSTRAINT semantic_evidence_admission_acquisition_id_fkey FOREIGN KEY (acquisition_id) REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE;


--
-- Name: semantic_interpretation semantic_interpretation_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_interpretation
    ADD CONSTRAINT semantic_interpretation_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: semantic_interpretation semantic_interpretation_frame_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_interpretation
    ADD CONSTRAINT semantic_interpretation_frame_id_fkey FOREIGN KEY (frame_id) REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE CASCADE;


--
-- Name: semantic_interpretation_role semantic_interpretation_role_child_frame_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_interpretation_role
    ADD CONSTRAINT semantic_interpretation_role_child_frame_id_fkey FOREIGN KEY (child_frame_id) REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE CASCADE;


--
-- Name: semantic_interpretation_role semantic_interpretation_role_interpretation_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_interpretation_role
    ADD CONSTRAINT semantic_interpretation_role_interpretation_id_fkey FOREIGN KEY (interpretation_id) REFERENCES aios.semantic_interpretation(interpretation_id) ON DELETE CASCADE;


--
-- Name: semantic_neighbor_candidate semantic_neighbor_candidate_neighbor_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_neighbor_candidate
    ADD CONSTRAINT semantic_neighbor_candidate_neighbor_proposition_id_fkey FOREIGN KEY (neighbor_proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_neighbor_candidate semantic_neighbor_candidate_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_neighbor_candidate
    ADD CONSTRAINT semantic_neighbor_candidate_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_neighbor_relation semantic_neighbor_relation_neighbor_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_neighbor_relation
    ADD CONSTRAINT semantic_neighbor_relation_neighbor_proposition_id_fkey FOREIGN KEY (neighbor_proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_neighbor_relation semantic_neighbor_relation_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_neighbor_relation
    ADD CONSTRAINT semantic_neighbor_relation_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_outlier_candidate semantic_outlier_candidate_nearest_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_outlier_candidate
    ADD CONSTRAINT semantic_outlier_candidate_nearest_proposition_id_fkey FOREIGN KEY (nearest_proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL;


--
-- Name: semantic_outlier_candidate semantic_outlier_candidate_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_outlier_candidate
    ADD CONSTRAINT semantic_outlier_candidate_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_outlier_candidate semantic_outlier_candidate_run_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_outlier_candidate
    ADD CONSTRAINT semantic_outlier_candidate_run_id_fkey FOREIGN KEY (run_id) REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE;


--
-- Name: semantic_reconciliation_receipt semantic_reconciliation_receipt_topology_edge_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_reconciliation_receipt
    ADD CONSTRAINT semantic_reconciliation_receipt_topology_edge_id_fkey FOREIGN KEY (topology_edge_id) REFERENCES aios.semantic_topology_edge(edge_id) ON DELETE SET NULL;


--
-- Name: semantic_reconciliation_receipt semantic_reconciliation_receipt_topology_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_reconciliation_receipt
    ADD CONSTRAINT semantic_reconciliation_receipt_topology_node_id_fkey FOREIGN KEY (topology_node_id) REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE SET NULL;


--
-- Name: semantic_structure_state semantic_structure_state_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_structure_state
    ADD CONSTRAINT semantic_structure_state_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_edge semantic_topology_edge_acquisition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_edge
    ADD CONSTRAINT semantic_topology_edge_acquisition_id_fkey FOREIGN KEY (acquisition_id) REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_edge semantic_topology_edge_assertion_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_edge
    ADD CONSTRAINT semantic_topology_edge_assertion_id_fkey FOREIGN KEY (assertion_id) REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_edge semantic_topology_edge_child_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_edge
    ADD CONSTRAINT semantic_topology_edge_child_node_id_fkey FOREIGN KEY (child_node_id) REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_edge semantic_topology_edge_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_edge
    ADD CONSTRAINT semantic_topology_edge_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_edge semantic_topology_edge_parent_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_edge
    ADD CONSTRAINT semantic_topology_edge_parent_node_id_fkey FOREIGN KEY (parent_node_id) REFERENCES aios.semantic_topology_node(topology_node_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_node semantic_topology_node_acquisition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_acquisition_id_fkey FOREIGN KEY (acquisition_id) REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_node semantic_topology_node_assertion_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_assertion_id_fkey FOREIGN KEY (assertion_id) REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_node semantic_topology_node_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_character_id_fkey FOREIGN KEY (character_id) REFERENCES aios.character_identity(character_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_node semantic_topology_node_character_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_character_instance_id_fkey FOREIGN KEY (character_instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_node semantic_topology_node_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_node semantic_topology_node_dag_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_dag_node_id_fkey FOREIGN KEY (dag_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: semantic_topology_node semantic_topology_node_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_node semantic_topology_node_source_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_source_id_fkey FOREIGN KEY (source_id) REFERENCES aios.source_identity(source_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_node semantic_topology_node_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL;


--
-- Name: semantic_topology_node semantic_topology_node_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_node
    ADD CONSTRAINT semantic_topology_node_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_projection semantic_topology_projection_acquisition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_projection
    ADD CONSTRAINT semantic_topology_projection_acquisition_id_fkey FOREIGN KEY (acquisition_id) REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_projection semantic_topology_projection_assertion_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_projection
    ADD CONSTRAINT semantic_topology_projection_assertion_id_fkey FOREIGN KEY (assertion_id) REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE CASCADE;


--
-- Name: semantic_topology_projection semantic_topology_projection_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_topology_projection
    ADD CONSTRAINT semantic_topology_projection_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE;


--
-- Name: semantic_validation_dependency semantic_validation_dependency_decision_type_decision_key_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.semantic_validation_dependency
    ADD CONSTRAINT semantic_validation_dependency_decision_type_decision_key_fkey FOREIGN KEY (decision_type, decision_key) REFERENCES aios.semantic_validation_decision(decision_type, decision_key) ON DELETE CASCADE;


--
-- Name: timeline timeline_source_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.timeline
    ADD CONSTRAINT timeline_source_id_fkey FOREIGN KEY (source_id) REFERENCES aios.source_identity(source_id) ON DELETE SET NULL;


--
-- Name: timeline timeline_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.timeline
    ADD CONSTRAINT timeline_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: world world_anchor_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_anchor_node_id_fkey FOREIGN KEY (anchor_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: world world_anchor_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_anchor_timeline_id_fkey FOREIGN KEY (anchor_timeline_id) REFERENCES aios.timeline(timeline_id);


--
-- Name: world world_canon_of_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_canon_of_world_id_fkey FOREIGN KEY (canon_of_world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: world_entity world_entity_character_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity
    ADD CONSTRAINT world_entity_character_instance_id_fkey FOREIGN KEY (character_instance_id) REFERENCES aios.character_instance(instance_id);


--
-- Name: world_entity_relation world_entity_relation_object_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity_relation
    ADD CONSTRAINT world_entity_relation_object_entity_id_fkey FOREIGN KEY (object_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: world_entity_relation world_entity_relation_subject_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity_relation
    ADD CONSTRAINT world_entity_relation_subject_entity_id_fkey FOREIGN KEY (subject_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: world_entity_relation world_entity_relation_valid_from_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity_relation
    ADD CONSTRAINT world_entity_relation_valid_from_node_id_fkey FOREIGN KEY (valid_from_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: world_entity_relation world_entity_relation_valid_to_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity_relation
    ADD CONSTRAINT world_entity_relation_valid_to_node_id_fkey FOREIGN KEY (valid_to_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: world_entity_relation world_entity_relation_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity_relation
    ADD CONSTRAINT world_entity_relation_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id);


--
-- Name: world_entity world_entity_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_entity
    ADD CONSTRAINT world_entity_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id);


--
-- Name: world_event world_event_actor_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event
    ADD CONSTRAINT world_event_actor_entity_id_fkey FOREIGN KEY (actor_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: world_event world_event_candidate_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event
    ADD CONSTRAINT world_event_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES aios.causal_candidate(candidate_id) ON DELETE SET NULL;


--
-- Name: world_event world_event_dag_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event
    ADD CONSTRAINT world_event_dag_node_id_fkey FOREIGN KEY (dag_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: world_event_exposure world_event_exposure_acquisition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event_exposure
    ADD CONSTRAINT world_event_exposure_acquisition_id_fkey FOREIGN KEY (acquisition_id) REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE SET NULL;


--
-- Name: world_event_exposure world_event_exposure_character_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event_exposure
    ADD CONSTRAINT world_event_exposure_character_node_id_fkey FOREIGN KEY (character_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: world_event_exposure world_event_exposure_character_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event_exposure
    ADD CONSTRAINT world_event_exposure_character_timeline_id_fkey FOREIGN KEY (character_timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: world_event_exposure world_event_exposure_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event_exposure
    ADD CONSTRAINT world_event_exposure_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE;


--
-- Name: world_event_exposure world_event_exposure_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event_exposure
    ADD CONSTRAINT world_event_exposure_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: world_event_exposure world_event_exposure_world_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event_exposure
    ADD CONSTRAINT world_event_exposure_world_node_id_fkey FOREIGN KEY (world_node_id) REFERENCES aios.dag_node(node_id) ON DELETE CASCADE;


--
-- Name: world_event_exposure world_event_exposure_world_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event_exposure
    ADD CONSTRAINT world_event_exposure_world_timeline_id_fkey FOREIGN KEY (world_timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: world_event world_event_instance_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event
    ADD CONSTRAINT world_event_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES aios.character_instance(instance_id);


--
-- Name: world_event world_event_target_entity_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event
    ADD CONSTRAINT world_event_target_entity_id_fkey FOREIGN KEY (target_entity_id) REFERENCES aios.world_entity(entity_id);


--
-- Name: world_event world_event_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event
    ADD CONSTRAINT world_event_timeline_id_fkey FOREIGN KEY (timeline_id) REFERENCES aios.timeline(timeline_id);


--
-- Name: world_event world_event_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_event
    ADD CONSTRAINT world_event_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id);


--
-- Name: world_lineage world_lineage_child_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_lineage
    ADD CONSTRAINT world_lineage_child_world_id_fkey FOREIGN KEY (child_world_id) REFERENCES aios.world(world_id);


--
-- Name: world_lineage world_lineage_parent_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_lineage
    ADD CONSTRAINT world_lineage_parent_world_id_fkey FOREIGN KEY (parent_world_id) REFERENCES aios.world(world_id);


--
-- Name: world_lineage world_lineage_split_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_lineage
    ADD CONSTRAINT world_lineage_split_id_fkey FOREIGN KEY (split_id) REFERENCES aios.world_split_candidate_world(split_id);


--
-- Name: world_memory_state world_memory_state_atom_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_memory_state
    ADD CONSTRAINT world_memory_state_atom_id_fkey FOREIGN KEY (atom_id) REFERENCES aios.semantic_atom(atom_id) ON DELETE CASCADE;


--
-- Name: world_memory_state world_memory_state_preferred_assertion_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_memory_state
    ADD CONSTRAINT world_memory_state_preferred_assertion_id_fkey FOREIGN KEY (preferred_assertion_id) REFERENCES aios.world_proposition_assertion(assertion_id) ON DELETE SET NULL;


--
-- Name: world_memory_state world_memory_state_preferred_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_memory_state
    ADD CONSTRAINT world_memory_state_preferred_proposition_id_fkey FOREIGN KEY (preferred_proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL;


--
-- Name: world_memory_state world_memory_state_resolved_through_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_memory_state
    ADD CONSTRAINT world_memory_state_resolved_through_node_id_fkey FOREIGN KEY (resolved_through_node_id) REFERENCES aios.dag_node(node_id) ON DELETE SET NULL;


--
-- Name: world_memory_state world_memory_state_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_memory_state
    ADD CONSTRAINT world_memory_state_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: world world_origin_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_origin_character_id_fkey FOREIGN KEY (origin_character_id) REFERENCES aios.character_identity(character_id);


--
-- Name: world world_parent_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_parent_world_id_fkey FOREIGN KEY (parent_world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: world_proposition_assertion world_proposition_assertion_generated_at_node_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_proposition_assertion
    ADD CONSTRAINT world_proposition_assertion_generated_at_node_id_fkey FOREIGN KEY (generated_at_node_id) REFERENCES aios.dag_node(node_id);


--
-- Name: world_proposition_assertion world_proposition_assertion_proposition_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_proposition_assertion
    ADD CONSTRAINT world_proposition_assertion_proposition_id_fkey FOREIGN KEY (proposition_id) REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE;


--
-- Name: world_proposition_assertion world_proposition_assertion_superseded_by_assertion_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_proposition_assertion
    ADD CONSTRAINT world_proposition_assertion_superseded_by_assertion_id_fkey FOREIGN KEY (superseded_by_assertion_id) REFERENCES aios.world_proposition_assertion(assertion_id);


--
-- Name: world_proposition_assertion world_proposition_assertion_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_proposition_assertion
    ADD CONSTRAINT world_proposition_assertion_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: world_rdf_projection world_rdf_projection_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_rdf_projection
    ADD CONSTRAINT world_rdf_projection_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: world world_root_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_root_world_id_fkey FOREIGN KEY (root_world_id) REFERENCES aios.world(world_id);


--
-- Name: world_rule world_rule_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_rule
    ADD CONSTRAINT world_rule_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: world_split_candidate_world world_split_candidate_world_parent_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_split_candidate_world
    ADD CONSTRAINT world_split_candidate_world_parent_world_id_fkey FOREIGN KEY (parent_world_id) REFERENCES aios.world(world_id);


--
-- Name: world_split_pressure world_split_pressure_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_split_pressure
    ADD CONSTRAINT world_split_pressure_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id);


--
-- Name: world_timeline_binding world_timeline_binding_objective_timeline_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_timeline_binding
    ADD CONSTRAINT world_timeline_binding_objective_timeline_id_fkey FOREIGN KEY (objective_timeline_id) REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE;


--
-- Name: world_timeline_binding world_timeline_binding_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_timeline_binding
    ADD CONSTRAINT world_timeline_binding_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


