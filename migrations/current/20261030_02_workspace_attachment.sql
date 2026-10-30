-- An operator may attach an existing workspace to its conversational instance.
-- Attached bindings never write to the conversation source; run events/receipts
-- retain workspace evidence independently. Existing separate environments persist.
BEGIN;
ALTER TABLE aios.workspace_binding ADD COLUMN attached boolean NOT NULL DEFAULT false;
ALTER TABLE aios.character_environment_link DROP CONSTRAINT character_environment_link_check;
COMMIT;
