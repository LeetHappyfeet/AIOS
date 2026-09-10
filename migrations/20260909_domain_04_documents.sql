-- AIOS domain migration: hierarchical document structure
-- Consolidates long-document units and metadata observations. Character
-- epistemic weighting lives in the epistemic/runtime domain.

BEGIN;

-- AIOS long-document + deterministic character epistemics
-- 2026-09-05


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

COMMIT;
