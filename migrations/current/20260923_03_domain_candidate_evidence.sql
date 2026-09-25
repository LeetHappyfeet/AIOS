-- Exact evidence for discovered corpus domain candidates.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.knowledge_domain_candidate_document (
    candidate_id uuid NOT NULL REFERENCES aios.knowledge_domain_candidate(candidate_id) ON DELETE CASCADE,
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (candidate_id, document_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_domain_candidate_document_doc
    ON aios.knowledge_domain_candidate_document(document_id, candidate_id);

COMMENT ON TABLE aios.knowledge_domain_candidate_document IS
'Unique document evidence for an unresolved structured domain identifier; prevents repeated ingestion of one document from inflating discovery counts.';

COMMIT;
