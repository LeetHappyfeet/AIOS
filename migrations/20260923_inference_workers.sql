-- Donated OpenAI-compatible inference workers and durable inference requests.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.inference_provider (
    provider_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_key text NOT NULL UNIQUE,
    display_name text NOT NULL,
    base_url text NOT NULL,
    model text NOT NULL,
    api_key_env text NULL,
    enabled boolean NOT NULL DEFAULT true,
    drain boolean NOT NULL DEFAULT false,
    max_concurrency integer NOT NULL DEFAULT 1 CHECK (max_concurrency > 0),
    context_window integer NULL CHECK (context_window IS NULL OR context_window > 0),
    timeout_seconds double precision NOT NULL DEFAULT 90 CHECK (timeout_seconds > 0),
    worker_classes text[] NOT NULL DEFAULT ARRAY[]::text[],
    capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'unknown'
        CHECK (status IN ('unknown','ready','busy','degraded','offline','disabled')),
    consecutive_failures integer NOT NULL DEFAULT 0,
    last_health_at timestamptz NULL,
    last_success_at timestamptz NULL,
    last_failure_at timestamptz NULL,
    cooldown_until timestamptz NULL,
    last_error text NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inference_provider_routing
    ON aios.inference_provider (enabled, drain, status, updated_at);

CREATE TABLE IF NOT EXISTS aios.inference_request (
    request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL
        REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    task_id uuid NULL
        REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL,
    provider_id uuid NULL
        REFERENCES aios.inference_provider(provider_id) ON DELETE SET NULL,
    worker_class text NOT NULL,
    model text NULL,
    context_state_version bigint NULL,
    hud_profile_name text NULL,
    prompt_hash text NOT NULL,
    allowed_actions jsonb NOT NULL DEFAULT '[]'::jsonb,
    output_schema jsonb NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','succeeded','failed','invalid','cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    response_text text NULL,
    response_json jsonb NULL,
    validation_error text NULL,
    error text NULL,
    latency_ms integer NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz NULL,
    completed_at timestamptz NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inference_request_task
    ON aios.inference_request (task_id, created_at)
    WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inference_request_status
    ON aios.inference_request (status, created_at);

COMMIT;
