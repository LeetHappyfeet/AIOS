-- Durable cognitive tasks and character action lifecycle.
-- This is the persistence boundary for agency; execution/inference remains separate.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_cognitive_task (
    task_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    parent_task_id uuid NULL
        REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL,
    task_type text NOT NULL,
    objective text NOT NULL,
    hud_profile_name text NULL,
    retrieval_focus text NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','waiting','succeeded','failed','cancelled')),
    priority integer NOT NULL DEFAULT 100,
    trigger_type text NULL,
    trigger_id text NULL,
    source_state_version bigint NULL,
    source_node_id uuid NULL REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    result jsonb NULL,
    error text NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz NULL,
    waiting_at timestamptz NULL,
    completed_at timestamptz NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_character_cognitive_task_instance_status
    ON aios.character_cognitive_task (instance_id, status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_character_cognitive_task_parent
    ON aios.character_cognitive_task (parent_task_id)
    WHERE parent_task_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS aios.character_action (
    action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    task_id uuid NULL
        REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL,
    parent_action_id uuid NULL
        REFERENCES aios.character_action(action_id) ON DELETE SET NULL,
    action_type text NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'proposed'
        CHECK (status IN (
            'proposed','validated','queued','running',
            'succeeded','failed','rejected','cancelled','timed_out'
        )),
    side_effect_class text NOT NULL DEFAULT 'read_only'
        CHECK (side_effect_class IN (
            'read_only','internal_write','external_reversible','external_sensitive'
        )),
    idempotency_key text NULL,
    expected_state_version bigint NULL,
    proposed_by text NULL,
    validated_at timestamptz NULL,
    queued_at timestamptz NULL,
    started_at timestamptz NULL,
    completed_at timestamptz NULL,
    result jsonb NULL,
    error text NULL,
    rejection_reason text NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instance_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_character_action_instance_status
    ON aios.character_action (instance_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_character_action_task
    ON aios.character_action (task_id, created_at)
    WHERE task_id IS NOT NULL;

COMMIT;
