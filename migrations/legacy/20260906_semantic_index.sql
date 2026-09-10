CREATE TABLE IF NOT EXISTS aios.semantic_vector_index_state (
    object_type text NOT NULL,
    object_key text NOT NULL,
    qdrant_collection text NOT NULL,
    embedding_model text NOT NULL,
    embedding_version text NOT NULL,
    vector_hash text,
    indexed_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    PRIMARY KEY (
        object_type, object_key, qdrant_collection,
        embedding_model, embedding_version
    )
);

CREATE INDEX IF NOT EXISTS semantic_vector_index_state_lookup_idx
ON aios.semantic_vector_index_state (object_type, indexed_at);

CREATE TABLE IF NOT EXISTS aios.semantic_neighbor_candidate (
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    neighbor_proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    similarity double precision NOT NULL,
    relation_hint text NOT NULL DEFAULT 'semantic_neighbor',
    status text NOT NULL DEFAULT 'candidate',
    embedding_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (proposition_id <> neighbor_proposition_id),
    PRIMARY KEY (proposition_id, neighbor_proposition_id, embedding_version)
);

CREATE INDEX IF NOT EXISTS semantic_neighbor_candidate_score_idx
ON aios.semantic_neighbor_candidate (similarity DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_structure_state (
    proposition_id uuid PRIMARY KEY REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    embedding_version text NOT NULL,
    analyzed_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE aios.semantic_neighbor_candidate IS
'Advisory vector-neighbor candidates only. Never authoritative for truth, world membership, branch membership, equivalence, or epistemic visibility.';
