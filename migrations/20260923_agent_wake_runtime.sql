-- Wake/event inbox and per-instance agent runtime.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_agent_runtime (
    instance_id uuid PRIMARY KEY
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'dormant'
        CHECK (state IN ('dormant','ready','thinking','acting','waiting','paused')),
    active_task_id uuid NULL
        REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL,
    active_action_id uuid NULL
        REFERENCES aios.character_action(action_id) ON DELETE SET NULL,
    heartbeat_enabled boolean NOT NULL DEFAULT false,
    heartbeat_interval_seconds integer NOT NULL DEFAULT 300
        CHECK (heartbeat_interval_seconds >= 60),
    next_heartbeat_at timestamptz NULL,
    last_wake_at timestamptz NULL,
    last_inference_at timestamptz NULL,
    last_activity_at timestamptz NULL,
    consecutive_wakes integer NOT NULL DEFAULT 0,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aios.character_wake_event (
    wake_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    event_type text NOT NULL,
    source_type text NULL,
    source_id text NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    priority integer NOT NULL DEFAULT 100,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','consumed','cancelled')),
    dedupe_key text NULL,
    available_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz NULL,
    consumed_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instance_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_character_wake_pending
    ON aios.character_wake_event (instance_id, status, priority, available_at, created_at);

COMMIT;
