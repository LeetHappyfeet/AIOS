-- AIOS long-document + deterministic character epistemics
-- 2026-09-05

BEGIN;

CREATE TABLE IF NOT EXISTS aios.document_unit (
    unit_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES aios.source_document(document_id) ON DELETE CASCADE,
    parent_unit_id uuid REFERENCES aios.document_unit(unit_id) ON DELETE CASCADE,
    node_id uuid REFERENCES aios.dag_node(node_id),
    unit_type text NOT NULL,
    unit_index integer NOT NULL,
    path text NOT NULL,
    title text,
    content text,
    start_char integer,
    end_char integer,
    depth integer NOT NULL DEFAULT 0,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (document_id, path)
);

CREATE INDEX IF NOT EXISTS idx_document_unit_doc_type
    ON aios.document_unit (document_id, unit_type, unit_index);

CREATE TABLE IF NOT EXISTS aios.document_metadata_observation (
    metadata_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES aios.source_document(document_id) ON DELETE CASCADE,
    field_type text NOT NULL,
    raw_value text NOT NULL,
    normalized_value text,
    source_unit_id uuid REFERENCES aios.document_unit(unit_id),
    source_location text,
    confidence double precision NOT NULL DEFAULT 0.5,
    extraction_method text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_document_metadata_doc_field
    ON aios.document_metadata_observation (document_id, field_type);

CREATE TABLE IF NOT EXISTS aios.character_epistemic_profile (
    character_id text PRIMARY KEY REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    skepticism double precision NOT NULL DEFAULT 0.5 CHECK (skepticism BETWEEN 0 AND 1),
    curiosity double precision NOT NULL DEFAULT 0.5 CHECK (curiosity BETWEEN 0 AND 1),
    authority_trust double precision NOT NULL DEFAULT 0.5 CHECK (authority_trust BETWEEN 0 AND 1),
    novelty_seeking double precision NOT NULL DEFAULT 0.5 CHECK (novelty_seeking BETWEEN 0 AND 1),
    emotional_reactivity double precision NOT NULL DEFAULT 0.5 CHECK (emotional_reactivity BETWEEN 0 AND 1),
    retention double precision NOT NULL DEFAULT 0.7 CHECK (retention BETWEEN 0 AND 1),
    source_trust jsonb NOT NULL DEFAULT '{}'::jsonb,
    topic_interest jsonb NOT NULL DEFAULT '{}'::jsonb,
    domain_expertise jsonb NOT NULL DEFAULT '{}'::jsonb,
    trait_weights jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE aios.character_proposition_knowledge
    ADD COLUMN IF NOT EXISTS base_confidence double precision,
    ADD COLUMN IF NOT EXISTS attention_weight double precision,
    ADD COLUMN IF NOT EXISTS trust_weight double precision,
    ADD COLUMN IF NOT EXISTS compatibility_weight double precision,
    ADD COLUMN IF NOT EXISTS retention_weight double precision,
    ADD COLUMN IF NOT EXISTS salience_weight double precision,
    ADD COLUMN IF NOT EXISTS effective_confidence double precision;

COMMIT;
