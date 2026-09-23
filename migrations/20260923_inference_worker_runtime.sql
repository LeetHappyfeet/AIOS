-- Resilient heterogeneous inference worker runtime.

BEGIN;

ALTER TABLE aios.inference_provider
    ADD COLUMN IF NOT EXISTS provider_type text NOT NULL DEFAULT 'openai_compatible',
    ADD COLUMN IF NOT EXISTS api_key_secret text NULL,
    ADD COLUMN IF NOT EXISTS strict_worker_classes boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS health_interval_seconds integer NOT NULL DEFAULT 15,
    ADD COLUMN IF NOT EXISTS offline_health_interval_seconds integer NOT NULL DEFAULT 60,
    ADD COLUMN IF NOT EXISTS protocol_failures integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_health_latency_ms integer NULL;

ALTER TABLE aios.inference_request
    ADD COLUMN IF NOT EXISTS leased_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS last_progress_at timestamptz NULL;

CREATE INDEX IF NOT EXISTS idx_inference_request_provider_running
    ON aios.inference_request (provider_id, lease_expires_at)
    WHERE status='running';

COMMIT;
