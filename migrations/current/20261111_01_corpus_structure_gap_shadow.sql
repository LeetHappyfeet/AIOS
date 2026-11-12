-- Shadow-only structural gap candidates. No ingestion, inference or RDF projection.
BEGIN;
CREATE TABLE IF NOT EXISTS aios.corpus_structure_gap (
    gap_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_key text NOT NULL REFERENCES aios.knowledge_domain(domain_key),
    trigger_kind text NOT NULL CHECK (trigger_kind IN ('semantic_unresolved_pair','dense_sparse_reference')),
    topic_a uuid REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    topic_b uuid REFERENCES aios.knowledge_topic(topic_id) ON DELETE CASCADE,
    anchor_section_id uuid REFERENCES aios.corpus_section(section_id) ON DELETE CASCADE,
    evidence_sections uuid[] NOT NULL DEFAULT '{}',
    evidence_documents uuid[] NOT NULL DEFAULT '{}',
    evidence_revision text NOT NULL,
    signals jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','stale','dismissed')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    scan_version text NOT NULL DEFAULT 'structure-gap-shadow-v1',
    CHECK (
        (trigger_kind='semantic_unresolved_pair'
            AND topic_a IS NOT NULL AND topic_b IS NOT NULL
            AND topic_a::text < topic_b::text AND anchor_section_id IS NULL)
        OR (trigger_kind='dense_sparse_reference'
            AND topic_a IS NULL AND topic_b IS NULL AND anchor_section_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_corpus_structure_pair
    ON aios.corpus_structure_gap (domain_key,topic_a,topic_b)
    WHERE trigger_kind='semantic_unresolved_pair';
CREATE UNIQUE INDEX IF NOT EXISTS uq_corpus_structure_dense
    ON aios.corpus_structure_gap (domain_key,anchor_section_id)
    WHERE trigger_kind='dense_sparse_reference';
CREATE INDEX IF NOT EXISTS ix_corpus_structure_gap_listing
    ON aios.corpus_structure_gap (domain_key,status,last_seen_at DESC);
COMMENT ON TABLE aios.corpus_structure_gap IS
'Advisory, source-revision-aware potential structural gaps. These are NOT established relations, inference requests, permissions, or world/character memory.';
COMMIT;
