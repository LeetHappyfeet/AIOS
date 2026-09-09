-- Semantic classification of cluster regions and boundaries.
-- Results are advisory structural interpretations only.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_boundary_classification (
    classification_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    cluster_a_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    cluster_b_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    classification text NOT NULL CHECK (
        classification IN (
            'SAME_REGION',
            'TOPIC_SPLIT',
            'TEMPORAL_TRANSITION',
            'STATE_TRANSITION',
            'NARRATIVE_SPLIT',
            'CONTRADICTION_CLUSTER',
            'EXPERIENTIAL_BRANCH_CANDIDATE',
            'WORLD_BRANCH_CANDIDATE',
            'UNRESOLVED'
        )
    ),
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    classifier_version text NOT NULL,
    status text NOT NULL DEFAULT 'candidate',
    feature_scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, cluster_a_id, cluster_b_id, classifier_version)
);

CREATE INDEX IF NOT EXISTS idx_semantic_boundary_classification_type
    ON aios.semantic_boundary_classification
       (classification, confidence DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_cluster_classification (
    classification_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    cluster_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    classification text NOT NULL CHECK (
        classification IN (
            'TOPIC_REGION',
            'STATE_SERIES',
            'EVENT_REGION',
            'MEMORY_REGION',
            'BELIEF_REGION',
            'RULE_REGION',
            'GOAL_REGION',
            'MIXED_REGION',
            'UNRESOLVED'
        )
    ),
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    classifier_version text NOT NULL,
    status text NOT NULL DEFAULT 'candidate',
    feature_scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, cluster_id, classifier_version)
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_classification_type
    ON aios.semantic_cluster_classification
       (classification, confidence DESC, created_at DESC);

COMMENT ON TABLE aios.semantic_boundary_classification IS
'Advisory interpretation of cluster boundaries. Branch candidate labels never create branches automatically.';

COMMENT ON TABLE aios.semantic_cluster_classification IS
'Advisory interpretation of semantic cluster contents for later topology routing and pruning.';

COMMIT;
