-- Corpus Discovery V2: rebuildable cold-vector and advisory topic coverage receipts.
-- Stage 2 does not change source admission, /world or /char truth.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.corpus_discovery_projection (
    section_id uuid PRIMARY KEY REFERENCES aios.corpus_section(section_id) ON DELETE CASCADE,
    source_fingerprint text NOT NULL,
    vector_collection text NOT NULL,
    embedding_model text NOT NULL,
    embedding_version text NOT NULL,
    projector_version text NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_corpus_discovery_projection_collection
    ON aios.corpus_discovery_projection(vector_collection, indexed_at);

CREATE TABLE IF NOT EXISTS aios.corpus_discovery_tombstone (
    section_id uuid PRIMARY KEY,
    removed_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION aios.queue_corpus_discovery_deletion()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO aios.corpus_discovery_tombstone(section_id,removed_at)
    VALUES(OLD.section_id,now()) ON CONFLICT(section_id)
    DO UPDATE SET removed_at=EXCLUDED.removed_at;
    RETURN OLD;
END $$;
DROP TRIGGER IF EXISTS trg_corpus_discovery_deletion ON aios.corpus_section;
CREATE TRIGGER trg_corpus_discovery_deletion AFTER DELETE ON aios.corpus_section
FOR EACH ROW EXECUTE FUNCTION aios.queue_corpus_discovery_deletion();

-- Candidate topic-to-section vector coverage is explicitly NOT confirmed.
ALTER TABLE aios.knowledge_topic_source
    ADD COLUMN IF NOT EXISTS similarity double precision,
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'candidate';
ALTER TABLE aios.knowledge_topic_source
    ADD CONSTRAINT knowledge_topic_source_similarity_check
    CHECK (similarity IS NULL OR (similarity >= -1 AND similarity <= 1));
ALTER TABLE aios.knowledge_topic_source
    ADD CONSTRAINT knowledge_topic_source_status_check
    CHECK (status IN ('candidate','confirmed','rejected'));
CREATE INDEX IF NOT EXISTS idx_topic_source_section_status
    ON aios.knowledge_topic_source(section_id,status,topic_id)
    WHERE section_id IS NOT NULL;

CREATE OR REPLACE FUNCTION aios.bump_vector_topic_source_revision()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        IF OLD.link_kind='vector_candidate' THEN
            UPDATE aios.knowledge_topic SET graph_revision=graph_revision+1,updated_at=now()
            WHERE topic_id=OLD.topic_id;
        END IF;
    ELSE
        IF NEW.link_kind='vector_candidate' THEN
            UPDATE aios.knowledge_topic SET graph_revision=graph_revision+1,updated_at=now()
            WHERE topic_id=NEW.topic_id;
        END IF;
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_vector_topic_source_revision ON aios.knowledge_topic_source;
CREATE TRIGGER trg_vector_topic_source_revision
AFTER INSERT OR DELETE ON aios.knowledge_topic_source
FOR EACH ROW EXECUTE FUNCTION aios.bump_vector_topic_source_revision();

CREATE TABLE IF NOT EXISTS aios.corpus_topic_link_state (
    section_id uuid PRIMARY KEY REFERENCES aios.corpus_section(section_id) ON DELETE CASCADE,
    source_fingerprint text NOT NULL,
    topic_epoch timestamptz NOT NULL,
    linker_version text NOT NULL,
    scanned_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE aios.corpus_discovery_projection IS
'Qdrant cold corpus v2 receipt. A source fingerprint changes when text/title/heading/facets/domain metadata changes.';
COMMENT ON TABLE aios.corpus_topic_link_state IS
'Bounded advisory corpus-to-topic candidate search receipt. Similarity never proves topical coverage or truth.';
COMMIT;
