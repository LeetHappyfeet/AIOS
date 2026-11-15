-- Optional, evidence-scoped character perspective. It never changes assignment hashes,
-- work admission, execution authority, or goal verification.
ALTER TABLE aios.workspace_run
    ADD COLUMN IF NOT EXISTS character_activity jsonb;
