-- Support bounded keyset discovery for vector and epistemic indexers.
-- Do not alter evidence, admission decisions, Qdrant receipts or RDF state.
-- These are plain CREATE INDEX statements for the existing transaction-based
-- migration runner. Apply during a maintenance window on large databases.
BEGIN;

CREATE INDEX IF NOT EXISTS idx_aios_proposition_vector_scan
    ON aios.proposition (created_at DESC, proposition_id DESC);

CREATE INDEX IF NOT EXISTS idx_aios_character_knowledge_vector_scan
    ON aios.character_proposition_knowledge (updated_at DESC, instance_id, proposition_id);

CREATE INDEX IF NOT EXISTS idx_aios_world_assertion_vector_scan
    ON aios.world_proposition_assertion (updated_at DESC, assertion_id);

-- Receipt-driven quarantine now walks object type and collection first.
CREATE INDEX IF NOT EXISTS idx_aios_vector_receipt_quarantine
    ON aios.semantic_vector_index_state
       (qdrant_collection, object_type, object_key);

COMMIT;
