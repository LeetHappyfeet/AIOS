BEGIN;

CREATE TABLE IF NOT EXISTS aios.message_cognitive_commit (
    commit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    node_id uuid NOT NULL REFERENCES aios.dag_node(node_id) ON DELETE CASCADE,
    timeline_id uuid,
    event_id bigint,
    character_id text NOT NULL,
    speaker_id text,
    speaker_role text,
    viewpoint_id text,
    interpreter_version text NOT NULL,
    source_text_hash text NOT NULL,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    committed_at timestamptz NOT NULL DEFAULT now(),
    enrichment_completed_at timestamptz,
    UNIQUE (instance_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_message_cognitive_commit_instance_event
    ON aios.message_cognitive_commit(instance_id, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_message_cognitive_commit_node
    ON aios.message_cognitive_commit(node_id);

CREATE TABLE IF NOT EXISTS aios.message_cognitive_unit (
    unit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    commit_id uuid NOT NULL REFERENCES aios.message_cognitive_commit(commit_id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    claim_kind text NOT NULL,
    text text NOT NULL,
    topic_key text NOT NULL,
    polarity smallint NOT NULL DEFAULT 1,
    salience double precision NOT NULL DEFAULT 0.5,
    confidence double precision NOT NULL DEFAULT 0.5,
    status text NOT NULL DEFAULT 'active',
    supersedes_unit_id uuid REFERENCES aios.message_cognitive_unit(unit_id),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (commit_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_message_cognitive_unit_commit_status
    ON aios.message_cognitive_unit(commit_id, status, salience DESC);
CREATE INDEX IF NOT EXISTS idx_message_cognitive_unit_topic
    ON aios.message_cognitive_unit(topic_key, claim_kind, created_at DESC);

ALTER TABLE aios.character_hud_readiness
    ADD COLUMN IF NOT EXISTS cognitive_ready_node_id uuid,
    ADD COLUMN IF NOT EXISTS cognitive_ready_event_id bigint,
    ADD COLUMN IF NOT EXISTS enrichment_ready_node_id uuid,
    ADD COLUMN IF NOT EXISTS enrichment_ready_event_id bigint;

-- Existing retrieval readiness represented the old exhaustive barrier. Preserve
-- it as the best available starting point for both cursors during upgrade.
UPDATE aios.character_hud_readiness
SET cognitive_ready_node_id = COALESCE(cognitive_ready_node_id, retrieval_ready_node_id),
    cognitive_ready_event_id = COALESCE(cognitive_ready_event_id, retrieval_ready_event_id),
    enrichment_ready_node_id = COALESCE(enrichment_ready_node_id, retrieval_ready_node_id),
    enrichment_ready_event_id = COALESCE(enrichment_ready_event_id, retrieval_ready_event_id)
WHERE retrieval_ready_node_id IS NOT NULL;

COMMIT;
