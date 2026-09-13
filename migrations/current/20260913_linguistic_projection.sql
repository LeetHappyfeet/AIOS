CREATE TABLE IF NOT EXISTS aios.linguistic_projection (
    section_id uuid PRIMARY KEY
        REFERENCES aios.document_section(section_id) ON DELETE CASCADE,
    parser_name text NOT NULL,
    parser_version text NOT NULL,
    projection_version text NOT NULL,
    content_sha256 text NOT NULL,
    doc_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS linguistic_projection_parser_idx
    ON aios.linguistic_projection (parser_name, parser_version, projection_version);
