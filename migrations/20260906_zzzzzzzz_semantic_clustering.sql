-- Advisory semantic clustering over proposition-neighbor geometry.
-- Clusters are structural candidates only; they do not create topics, branches,
-- worlds, truth assertions, or character knowledge.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_cluster_run (
    run_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    embedding_version text NOT NULL,
    algorithm_version text NOT NULL,
    core_threshold double precision NOT NULL,
    attach_threshold double precision NOT NULL,
    min_cluster_size integer NOT NULL,
    neighbor_watermark timestamptz,
    cluster_count integer NOT NULL DEFAULT 0,
    outlier_count integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL DEFAULT 'running',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_run_latest
    ON aios.semantic_cluster_run (embedding_version, completed_at DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_cluster_candidate (
    cluster_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    embedding_version text NOT NULL,
    algorithm_version text NOT NULL,
    member_count integer NOT NULL,
    internal_edge_count integer NOT NULL DEFAULT 0,
    density double precision NOT NULL DEFAULT 0.0,
    cohesion double precision NOT NULL DEFAULT 0.0,
    boundary_strength double precision NOT NULL DEFAULT 0.0,
    separation double precision NOT NULL DEFAULT 0.0,
    status text NOT NULL DEFAULT 'candidate',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_candidate_run
    ON aios.semantic_cluster_candidate (run_id, cohesion DESC);
CREATE INDEX IF NOT EXISTS idx_semantic_cluster_candidate_status
    ON aios.semantic_cluster_candidate (embedding_version, status, cohesion DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_cluster_membership (
    cluster_id uuid NOT NULL REFERENCES aios.semantic_cluster_candidate(cluster_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    membership_kind text NOT NULL CHECK (membership_kind IN ('core','fringe')),
    affinity double precision NOT NULL DEFAULT 0.0,
    internal_degree integer NOT NULL DEFAULT 0,
    strongest_neighbor_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    strongest_similarity double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, proposition_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_cluster_membership_proposition
    ON aios.semantic_cluster_membership (proposition_id, affinity DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_outlier_candidate (
    run_id uuid NOT NULL REFERENCES aios.semantic_cluster_run(run_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    embedding_version text NOT NULL,
    reason text NOT NULL,
    nearest_similarity double precision,
    nearest_proposition_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'candidate',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, proposition_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_outlier_candidate_prop
    ON aios.semantic_outlier_candidate (proposition_id, created_at DESC);

COMMENT ON TABLE aios.semantic_cluster_candidate IS
'Advisory vector-geometry clusters. Classification into topic, state transition, narrative split, or branch requires later semantic/context validation.';

COMMENT ON TABLE aios.semantic_outlier_candidate IS
'Advisory semantic outliers. Never delete, reject, or demote propositions solely from this table.';

COMMIT;
