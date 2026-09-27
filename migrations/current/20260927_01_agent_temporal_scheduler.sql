-- Durable temporal triggers for agent timers and future scheduling.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_temporal_trigger (
    trigger_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    trigger_type text NOT NULL DEFAULT 'timer'
        CHECK (trigger_type IN ('timer','schedule','deadline','timeout','condition_timeout')),
    clock_type text NOT NULL DEFAULT 'wall'
        CHECK (clock_type IN ('wall','simulation')),
    status text NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled','firing','fired','cancelled','completed')),
    due_at timestamptz NOT NULL,
    interval_seconds integer NULL CHECK (interval_seconds IS NULL OR interval_seconds > 0),
    timezone text NULL,
    task_id uuid NULL REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL,
    action_id uuid NULL REFERENCES aios.character_action(action_id) ON DELETE SET NULL,
    event_type text NOT NULL DEFAULT 'TIMER_DUE',
    reason text NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    priority integer NOT NULL DEFAULT 100,
    dedupe_key text NULL,
    fired_at timestamptz NULL,
    cancelled_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instance_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_character_temporal_trigger_due
    ON aios.character_temporal_trigger (status, due_at, priority)
    WHERE status='scheduled';

CREATE INDEX IF NOT EXISTS idx_character_temporal_trigger_instance
    ON aios.character_temporal_trigger (instance_id, status, due_at);

COMMIT;
