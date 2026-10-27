-- Workspace assignments are scoped independently of generic remote worker claims.
CREATE TABLE aios.workspace_binding (
    binding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_key text NOT NULL UNIQUE,
    worker_key text NOT NULL,
    instance_id uuid NOT NULL UNIQUE REFERENCES aios.character_instance(instance_id),
    character_id text NOT NULL,
    session_id uuid NOT NULL REFERENCES aios.session(session_id),
    user_name text NOT NULL,
    scope_key text NOT NULL,
    timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id),
    last_node_id uuid,
    root_hint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE aios.workspace_run (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    binding_id uuid NOT NULL REFERENCES aios.workspace_binding(binding_id),
    action_id uuid NOT NULL UNIQUE REFERENCES aios.character_action(action_id),
    request_id uuid NOT NULL,
    objective text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','cancelled','interrupted')),
    claim_token uuid,
    lease_expires_at timestamptz,
    result jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(binding_id,request_id)
);
CREATE UNIQUE INDEX workspace_one_running ON aios.workspace_run(binding_id) WHERE status='running';
CREATE TABLE aios.workspace_receipt (
    run_id uuid NOT NULL REFERENCES aios.workspace_run(run_id),
    event_key uuid NOT NULL,
    payload_hash text NOT NULL,
    response jsonb NOT NULL,
    PRIMARY KEY(run_id,event_key)
);
