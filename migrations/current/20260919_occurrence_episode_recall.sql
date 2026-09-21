-- Occurrence-first semantic events and derived episodic recall
-- 2026-09-19
BEGIN;

DROP INDEX IF EXISTS aios.ux_semantic_event_membership_active_proposition;

CREATE UNIQUE INDEX IF NOT EXISTS ux_semantic_event_membership_active_observation
    ON aios.semantic_event_membership (observation_id)
    WHERE status='active' AND observation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_semantic_event_membership_active_proposition
    ON aios.semantic_event_membership (proposition_id, status);

CREATE TABLE IF NOT EXISTS aios.semantic_episode (
    semantic_episode_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    episode_key text NOT NULL UNIQUE,
    world_id uuid REFERENCES aios.world(world_id) ON DELETE CASCADE,
    timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    start_dag_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    end_dag_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded')),
    confidence double precision NOT NULL DEFAULT 0.5
        CHECK (confidence BETWEEN 0.0 AND 1.0),
    resolver_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_semantic_episode_coordinate
    ON aios.semantic_episode (world_id, timeline_id, start_dag_node_id, status);

CREATE TABLE IF NOT EXISTS aios.semantic_episode_membership (
    semantic_episode_id uuid NOT NULL
        REFERENCES aios.semantic_episode(semantic_episode_id) ON DELETE CASCADE,
    semantic_event_id uuid NOT NULL
        REFERENCES aios.semantic_event(semantic_event_id) ON DELETE CASCADE,
    ordinal integer NOT NULL DEFAULT 0,
    membership_confidence double precision NOT NULL DEFAULT 1.0
        CHECK (membership_confidence BETWEEN 0.0 AND 1.0),
    assigned_by text NOT NULL,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (semantic_episode_id, semantic_event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_semantic_episode_membership_active_event
    ON aios.semantic_episode_membership (semantic_event_id)
    WHERE status='active';

COMMENT ON TABLE aios.semantic_episode IS
'Derived episodic grouping of canonical semantic occurrences. Disposable cognition structure; never source truth.';
COMMENT ON TABLE aios.semantic_episode_membership IS
'Canonical semantic events participating in a derived episode.';

COMMIT;
