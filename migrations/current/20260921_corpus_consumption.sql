-- Cold corpus and intentional character consumption.
-- Corpus rows are searchable source material only: they do not create ingest
-- events, claims, propositions, RDF, world assertions, or character knowledge.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.corpus_document (
    document_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    source_id text REFERENCES aios.source_identity(source_id) ON DELETE SET NULL,
    document_kind text NOT NULL DEFAULT 'document',
    title text,
    author text,
    source_uri text,
    language text,
    content_hash text NOT NULL UNIQUE,
    published_at timestamptz,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_corpus_document_source
    ON aios.corpus_document (source_id, created_at DESC)
    WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS aios.corpus_section (
    section_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    section_order integer NOT NULL,
    section_path text,
    heading text,
    content text NOT NULL,
    content_hash text NOT NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, section_order),
    UNIQUE (document_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_corpus_section_document
    ON aios.corpus_section (document_id, section_order);

CREATE TABLE IF NOT EXISTS aios.source_consumption (
    consumption_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    section_id uuid NOT NULL REFERENCES aios.corpus_section(section_id) ON DELETE CASCADE,
    mode text NOT NULL CHECK (mode IN ('read','research','taught','import')),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','ingested','failed')),
    ingest_event_id bigint REFERENCES aios.ingest_event(event_id) ON DELETE SET NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz,
    error text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (instance_id, section_id, mode)
);

CREATE INDEX IF NOT EXISTS idx_source_consumption_instance
    ON aios.source_consumption (instance_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_consumption_pending
    ON aios.source_consumption (requested_at)
    WHERE status='pending';

ALTER TABLE aios.knowledge_acquisition_event
    ADD COLUMN IF NOT EXISTS consumption_id uuid
        REFERENCES aios.source_consumption(consumption_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_knowledge_acquisition_consumption
    ON aios.knowledge_acquisition_event (consumption_id)
    WHERE consumption_id IS NOT NULL;

COMMENT ON TABLE aios.corpus_document IS
'Cold searchable source material. Presence here does not imply semantic ingestion, world truth, or character knowledge.';
COMMENT ON TABLE aios.corpus_section IS
'Searchable cold-corpus unit. Indexed for discovery without entering the semantic pipeline until explicitly consumed.';
COMMENT ON TABLE aios.source_consumption IS
'Explicit epistemic boundary recording that one character instance consumed one cold-corpus section.';

COMMIT;
