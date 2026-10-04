-- AIOS Topic Atlas V1: advisory discovery catalogue, not an epistemic authority.
-- All entries are rebuildable. Never promote mentions/relationships into /world or /char.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.knowledge_topic (
    topic_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_key text NOT NULL UNIQUE,
    namespace text NOT NULL,
    topic_kind text NOT NULL CHECK (topic_kind IN ('domain','entity','concept')),
    normalized_label text NOT NULL,
    display_label text NOT NULL,
    description text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate','registered','organized','retired')),
    visibility text NOT NULL CHECK (visibility IN ('catalog','private')),
    owner_character_id text,
    vector_revision bigint NOT NULL DEFAULT 1,
    graph_revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (namespace, topic_kind, normalized_label),
    CHECK (length(topic_key) > 0 AND length(namespace) > 0),
    CHECK (visibility = 'private' OR owner_character_id IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_topic_scope ON aios.knowledge_topic
    (namespace, status, topic_kind);

CREATE TABLE IF NOT EXISTS aios.knowledge_topic_alias (
    topic_id uuid NOT NULL REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    normalized_alias text NOT NULL,
    display_alias text NOT NULL,
    alias_kind text NOT NULL DEFAULT 'observed',
    source_kind text NOT NULL,
    source_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS aios.knowledge_topic_mention (
    topic_id uuid NOT NULL REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    source_kind text NOT NULL,
    source_key text NOT NULL,
    origin_key text NOT NULL,
    source_revision text NOT NULL,
    claim_id uuid,
    instance_id uuid,
    character_id text,
    world_id uuid,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, source_kind, source_key)
);
CREATE INDEX IF NOT EXISTS idx_topic_mention_origin ON aios.knowledge_topic_mention
    (topic_id, origin_key);
CREATE INDEX IF NOT EXISTS idx_topic_mention_source ON aios.knowledge_topic_mention
    (source_kind, source_key);

CREATE TABLE IF NOT EXISTS aios.knowledge_topic_relation (
    source_topic_id uuid NOT NULL REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    target_topic_id uuid NOT NULL REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    relation_kind text NOT NULL
        CHECK (relation_kind IN ('associated','broader','narrower','prerequisite')),
    status text NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate','verified','rejected')),
    source_kind text NOT NULL,
    source_key text NOT NULL,
    source_revision text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_topic_id,target_topic_id,relation_kind,source_kind,source_key),
    CHECK (source_topic_id <> target_topic_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_relation_target
    ON aios.knowledge_topic_relation(target_topic_id,relation_kind,status);

CREATE TABLE IF NOT EXISTS aios.knowledge_topic_source (
    topic_id uuid NOT NULL REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    section_id uuid REFERENCES aios.corpus_section(section_id) ON DELETE CASCADE,
    link_kind text NOT NULL,
    source_key text NOT NULL,
    source_revision text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, link_kind, source_key)
);
CREATE INDEX IF NOT EXISTS idx_topic_source_document
    ON aios.knowledge_topic_source(document_id,topic_id);

CREATE TABLE IF NOT EXISTS aios.knowledge_topic_interest (
    topic_id uuid NOT NULL REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    character_id text NOT NULL,
    distinct_origin_count bigint NOT NULL DEFAULT 0,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(topic_id,character_id)
);

CREATE TABLE IF NOT EXISTS aios.knowledge_topic_discovery_receipt (
    source_kind text NOT NULL,
    source_key text NOT NULL,
    source_revision text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('collected','skipped')),
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source_kind,source_key)
);

CREATE TABLE IF NOT EXISTS aios.knowledge_topic_projection (
    topic_id uuid PRIMARY KEY REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    vector_revision bigint NOT NULL DEFAULT 0,
    vector_hash text,
    embedding_model text,
    embedding_version text,
    vector_collection text,
    vector_projected_at timestamptz,
    graph_revision bigint NOT NULL DEFAULT 0,
    graph_hash text,
    graph_dataset text,
    graph_iri text,
    graph_projected_at timestamptz,
    last_error text
);

COMMENT ON TABLE aios.knowledge_topic IS 'Advisory topic identity catalogue. Candidate existence does not imply proposition integrity or truth.';
COMMENT ON TABLE aios.knowledge_topic_mention IS 'Deduplicated source occurrence, including private character scope; never knowledge acquisition.';
COMMENT ON TABLE aios.knowledge_topic_relation IS 'Candidate topic navigation only, independent of authoritative /world or /char relations.';
COMMENT ON TABLE aios.knowledge_topic_projection IS 'Rebuildable topic-only Fuseki/Qdrant projection receipts; backend acknowledgment precedes receipt.';
COMMIT;
