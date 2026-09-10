-- Pairwise semantic interpretation of Qdrant proposition neighbors.
-- These relations are advisory and may guide clustering/topology, but do not
-- replace proposition identity, conflicts, RDF, or epistemic authority.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_neighbor_relation (
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    neighbor_proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    embedding_version text NOT NULL,
    relation text NOT NULL CHECK (
        relation IN (
            'EQUIVALENT',
            'REFINES',
            'CONTRADICTS',
            'SAME_TOPIC',
            'SAME_EVENT',
            'RELATED',
            'UNRESOLVED'
        )
    ),
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    classifier_version text NOT NULL,
    status text NOT NULL DEFAULT 'candidate',
    features jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (proposition_id <> neighbor_proposition_id),
    PRIMARY KEY (
        proposition_id,
        neighbor_proposition_id,
        embedding_version,
        classifier_version
    )
);

CREATE INDEX IF NOT EXISTS idx_semantic_neighbor_relation_type
    ON aios.semantic_neighbor_relation
       (embedding_version, relation, confidence DESC);

COMMENT ON TABLE aios.semantic_neighbor_relation IS
'Advisory pairwise semantic interpretation of vector-neighbor propositions.';

COMMIT;
