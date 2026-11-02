-- Stable workers are independent of chats. Registration is not authorization.
BEGIN;
CREATE TABLE aios.workspace_registration (
    registration_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_key text NOT NULL UNIQUE,
    worker_key text NOT NULL,
    character_id text NOT NULL,
    user_name text NOT NULL,
    scope_key text NOT NULL,
    root_hint text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    disposition text NOT NULL DEFAULT 'require_approval'
        CHECK (disposition IN ('allow','require_approval','deny')),
    last_seen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- One authorized destination per character/human/scope; never guess between hosts.
CREATE UNIQUE INDEX workspace_registration_authorized_owner
    ON aios.workspace_registration(character_id,user_name,scope_key) WHERE enabled;
ALTER TABLE aios.workspace_binding ADD COLUMN registration_id uuid
    REFERENCES aios.workspace_registration(registration_id);
CREATE INDEX workspace_binding_registration ON aios.workspace_binding(registration_id);
COMMIT;
