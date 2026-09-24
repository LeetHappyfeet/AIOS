BEGIN;
ALTER TABLE aios.character_cognitive_operation
  ADD COLUMN IF NOT EXISTS source_timeline_id uuid;
COMMIT;
