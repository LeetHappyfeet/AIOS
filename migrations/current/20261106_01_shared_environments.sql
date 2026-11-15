-- Physical environment identity is separate from each character's standing grant.
-- Existing registrations remain valid; upgraded clients adopt their environment
-- identity on the next registration without replacing bindings or authority.
BEGIN;
ALTER TABLE aios.workspace_registration ADD COLUMN environment_id uuid;
ALTER TABLE aios.workspace_registration ADD COLUMN administration_enabled boolean NOT NULL DEFAULT false;
CREATE INDEX workspace_registration_environment ON aios.workspace_registration(environment_id);
COMMIT;
