-- Preserve corpus provenance when identical content appears at multiple source URLs.
BEGIN;

ALTER TABLE aios.corpus_document
    DROP CONSTRAINT IF EXISTS corpus_document_content_hash_key;

CREATE INDEX IF NOT EXISTS idx_corpus_document_content_hash
    ON aios.corpus_document (content_hash);

CREATE UNIQUE INDEX IF NOT EXISTS uq_corpus_document_source_uri_hash
    ON aios.corpus_document (source_id, source_uri, content_hash)
    WHERE source_id IS NOT NULL AND source_uri IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_corpus_document_source_no_uri_hash
    ON aios.corpus_document (source_id, content_hash)
    WHERE source_id IS NOT NULL AND source_uri IS NULL;

COMMENT ON COLUMN aios.corpus_document.content_hash IS
'Content fingerprint for comparison/reuse. It is not document identity: identical bytes from different sources/URLs retain separate provenance rows.';

COMMIT;
