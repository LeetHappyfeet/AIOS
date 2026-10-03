-- Preserve the start of a dirty interval so continuously mutating scopes
-- receive publication even when the regular quiet-period debounce never fires.
ALTER TABLE aios.semantic_scope_projection_state
    ADD COLUMN IF NOT EXISTS first_dirty_at timestamptz;
UPDATE aios.semantic_scope_projection_state
SET first_dirty_at = COALESCE(projected_at, dirty_at, now())
WHERE dirty_version > projected_version
  AND first_dirty_at IS NULL;
