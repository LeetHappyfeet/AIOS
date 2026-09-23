-- External integration gateway, approvals, and character-to-character delivery.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.external_integration (
    integration_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_key text NOT NULL UNIQUE,
    integration_type text NOT NULL,
    display_name text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    base_url text NULL,
    auth_env text NULL,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aios.external_event_receipt (
    receipt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_id uuid NOT NULL REFERENCES aios.external_integration(integration_id) ON DELETE CASCADE,
    external_event_id text NOT NULL,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (integration_id, external_event_id, instance_id)
);

CREATE TABLE IF NOT EXISTS aios.character_action_policy (
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    action_type text NOT NULL,
    disposition text NOT NULL DEFAULT 'require_approval'
        CHECK (disposition IN ('allow','require_approval','deny')),
    updated_by text NOT NULL DEFAULT 'operator',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, action_type)
);

CREATE TABLE IF NOT EXISTS aios.character_action_approval (
    approval_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id uuid NOT NULL UNIQUE REFERENCES aios.character_action(action_id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','denied','cancelled')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz NULL,
    decided_by text NULL,
    reason text NULL
);

CREATE TABLE IF NOT EXISTS aios.external_action_delivery (
    delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id uuid NOT NULL REFERENCES aios.character_action(action_id) ON DELETE CASCADE,
    integration_id uuid NOT NULL REFERENCES aios.external_integration(integration_id) ON DELETE CASCADE,
    correlation_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','sent','succeeded','failed','cancelled')),
    request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz NULL,
    UNIQUE (integration_id, correlation_key)
);

CREATE INDEX IF NOT EXISTS idx_external_delivery_pending
    ON aios.external_action_delivery (status, created_at);

COMMIT;
