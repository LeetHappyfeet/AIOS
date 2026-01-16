--
-- PostgreSQL database dump
--

\restrict gV7nZjisAboGbrwS60UdaIImCBPclchBurTszcDdugu1lgacMD7g5iCuXqnTxIl

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
-- Name: actor_type; Type: TYPE; Schema: aios; Owner: -
--

CREATE TYPE aios.actor_type AS ENUM (
    'user',
    'character',
    'agent',
    'system',
    'tool'
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
    'paragraph'
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


SET default_tablespace = '';

SET default_table_access_method = heap;

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
    current_world_id uuid
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
    created_at timestamp with time zone DEFAULT now() NOT NULL
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
-- Name: claim_provenance; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.claim_provenance (
    claim_id uuid NOT NULL,
    document_id uuid NOT NULL,
    citation text,
    source_weight real DEFAULT 0.5 NOT NULL
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
    origin aios.node_origin DEFAULT 'agent_action'::aios.node_origin NOT NULL
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
    node_id uuid
);


--
-- Name: extracted_sentence; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.extracted_sentence (
    sentence_id uuid DEFAULT gen_random_uuid() NOT NULL,
    section_id uuid NOT NULL,
    sentence_index integer NOT NULL,
    sentence_text text NOT NULL,
    citation text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
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
    process_error text
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
    updated_at timestamp with time zone DEFAULT now() NOT NULL
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
-- Name: section_cluster_assignment; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.section_cluster_assignment (
    split_id uuid NOT NULL,
    section_id uuid NOT NULL,
    cluster_label text NOT NULL,
    score_to_centroid double precision
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
-- Name: timeline; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.timeline (
    timeline_id uuid DEFAULT gen_random_uuid() NOT NULL,
    world_id uuid NOT NULL,
    name text DEFAULT 'main'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    character_id text,
    user_name text NOT NULL,
    scope_key text DEFAULT 'default'::text NOT NULL,
    session_id uuid
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
-- Name: vector_index_state; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.vector_index_state (
    section_id uuid NOT NULL,
    qdrant_collection text NOT NULL,
    indexed_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding_model text NOT NULL,
    embedding_version text NOT NULL,
    vector_hash text,
    last_error text
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
    canon_of_world_id uuid
);


--
-- Name: world_split_candidate; Type: TABLE; Schema: aios; Owner: -
--

CREATE TABLE aios.world_split_candidate (
    split_id uuid NOT NULL,
    seed_section_id uuid NOT NULL,
    window_start timestamp with time zone NOT NULL,
    window_end timestamp with time zone NOT NULL,
    cluster_count integer NOT NULL,
    cluster_a jsonb NOT NULL,
    cluster_b jsonb NOT NULL,
    centroid_distance double precision NOT NULL,
    boundary_pairs jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
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
-- Name: character_alias character_alias_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_alias
    ADD CONSTRAINT character_alias_pkey PRIMARY KEY (alias);


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
-- Name: claim_candidate claim_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_candidate
    ADD CONSTRAINT claim_candidate_pkey PRIMARY KEY (claim_id);


--
-- Name: claim_provenance claim_provenance_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_provenance
    ADD CONSTRAINT claim_provenance_pkey PRIMARY KEY (claim_id, document_id);


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
-- Name: extracted_sentence extracted_sentence_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.extracted_sentence
    ADD CONSTRAINT extracted_sentence_pkey PRIMARY KEY (sentence_id);


--
-- Name: ingest_event ingest_event_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.ingest_event
    ADD CONSTRAINT ingest_event_pkey PRIMARY KEY (event_id);


--
-- Name: memory_item memory_item_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.memory_item
    ADD CONSTRAINT memory_item_pkey PRIMARY KEY (memory_id);


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
-- Name: rdf_promotion_log rdf_promotion_log_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.rdf_promotion_log
    ADD CONSTRAINT rdf_promotion_log_pkey PRIMARY KEY (promotion_id);


--
-- Name: section_cluster_assignment section_cluster_assignment_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.section_cluster_assignment
    ADD CONSTRAINT section_cluster_assignment_pkey PRIMARY KEY (split_id, section_id);


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
-- Name: timeline timeline_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.timeline
    ADD CONSTRAINT timeline_pkey PRIMARY KEY (timeline_id);


--
-- Name: timeline timeline_world_id_name_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.timeline
    ADD CONSTRAINT timeline_world_id_name_key UNIQUE (world_id, name);


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
-- Name: vector_index_state vector_index_state_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.vector_index_state
    ADD CONSTRAINT vector_index_state_pkey PRIMARY KEY (section_id);


--
-- Name: world world_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_pkey PRIMARY KEY (world_id);


--
-- Name: world_split_candidate world_split_candidate_pkey; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world_split_candidate
    ADD CONSTRAINT world_split_candidate_pkey PRIMARY KEY (split_id);


--
-- Name: world world_world_key_key; Type: CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_world_key_key UNIQUE (world_key);


--
-- Name: idx_char_instance_lookup; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_char_instance_lookup ON aios.character_instance USING btree (character_id, world_id, owner_user_id);


--
-- Name: idx_character_alias_character; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_alias_character ON aios.character_alias USING btree (character_id);


--
-- Name: idx_character_identity_canon; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_identity_canon ON aios.character_identity USING btree (canon);


--
-- Name: idx_character_identity_franchise; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_character_identity_franchise ON aios.character_identity USING btree (franchise);


--
-- Name: idx_claim_candidate_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_candidate_status ON aios.claim_candidate USING btree (status);


--
-- Name: idx_claim_candidate_subject; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_claim_candidate_subject ON aios.claim_candidate USING btree (subject);


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
-- Name: idx_document_section_document; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_document_section_document ON aios.document_section USING btree (document_id);


--
-- Name: idx_extracted_sentence_section; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_extracted_sentence_section ON aios.extracted_sentence USING btree (section_id);


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
-- Name: idx_ingest_event_session_time; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_session_time ON aios.ingest_event USING btree (session_id, created_at DESC);


--
-- Name: idx_ingest_event_status; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_ingest_event_status ON aios.ingest_event USING btree (process_status, created_at DESC);


--
-- Name: idx_memory_thread_root; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_memory_thread_root ON aios.memory_item USING btree (thread_root_node_id);


--
-- Name: idx_memory_timeline_created; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_memory_timeline_created ON aios.memory_item USING btree (timeline_id, created_at DESC);


--
-- Name: idx_pipeline_job_pick; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_pipeline_job_pick ON aios.pipeline_job USING btree (status, priority, run_after, created_at);


--
-- Name: idx_rdf_promotion_claim; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_rdf_promotion_claim ON aios.rdf_promotion_log USING btree (claim_id);


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
-- Name: idx_timeline_character_created; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_timeline_character_created ON aios.timeline USING btree (character_id, created_at DESC);


--
-- Name: idx_timeline_lookup; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_timeline_lookup ON aios.timeline USING btree (character_id, user_name, scope_key, created_at DESC);


--
-- Name: idx_timeline_world; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_timeline_world ON aios.timeline USING btree (world_id);


--
-- Name: idx_vector_index_state_collection; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_vector_index_state_collection ON aios.vector_index_state USING btree (qdrant_collection);


--
-- Name: idx_world_canon; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_canon ON aios.world USING btree (canon_of_world_id);


--
-- Name: idx_world_parent; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_parent ON aios.world USING btree (parent_world_id);


--
-- Name: idx_world_type; Type: INDEX; Schema: aios; Owner: -
--

CREATE INDEX idx_world_type ON aios.world USING btree (world_type);


--
-- Name: character_alias character_alias_character_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_alias
    ADD CONSTRAINT character_alias_character_id_fkey FOREIGN KEY (character_id) REFERENCES aios.character_identity(character_id) ON DELETE CASCADE;


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
-- Name: character_instance character_instance_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES aios.user_identity(user_id) ON DELETE SET NULL;


--
-- Name: character_instance character_instance_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.character_instance
    ADD CONSTRAINT character_instance_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: claim_candidate claim_candidate_sentence_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.claim_candidate
    ADD CONSTRAINT claim_candidate_sentence_id_fkey FOREIGN KEY (sentence_id) REFERENCES aios.extracted_sentence(sentence_id) ON DELETE CASCADE;


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
-- Name: rdf_promotion_log rdf_promotion_log_claim_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.rdf_promotion_log
    ADD CONSTRAINT rdf_promotion_log_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES aios.claim_candidate(claim_id);


--
-- Name: timeline timeline_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.timeline
    ADD CONSTRAINT timeline_world_id_fkey FOREIGN KEY (world_id) REFERENCES aios.world(world_id) ON DELETE CASCADE;


--
-- Name: world world_canon_of_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_canon_of_world_id_fkey FOREIGN KEY (canon_of_world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- Name: world world_parent_world_id_fkey; Type: FK CONSTRAINT; Schema: aios; Owner: -
--

ALTER TABLE ONLY aios.world
    ADD CONSTRAINT world_parent_world_id_fkey FOREIGN KEY (parent_world_id) REFERENCES aios.world(world_id) ON DELETE SET NULL;


--
-- PostgreSQL database dump complete
--

\unrestrict gV7nZjisAboGbrwS60UdaIImCBPclchBurTszcDdugu1lgacMD7g5iCuXqnTxIl

