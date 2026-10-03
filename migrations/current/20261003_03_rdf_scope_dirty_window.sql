-- Preserve the start of a dirty interval so continuously mutating scopes
-- receive publication even when the regular quiet-period debounce never fires.
ALTER TABLE aios.semantic_scope_projection_state
    ADD COLUMN IF NOT EXISTS first_dirty_at timestamptz;
UPDATE aios.semantic_scope_projection_state
SET first_dirty_at = COALESCE(projected_at, dirty_at, now())
WHERE dirty_version > projected_version
  AND first_dirty_at IS NULL;

-- Invalidation can also originate from a character-belief SQL trigger.
CREATE OR REPLACE FUNCTION aios.mark_belief_scope_dirty() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_instance_id uuid;
    v_character_id text;
    v_scope_key text;
BEGIN
    IF TG_OP='DELETE' THEN
        v_instance_id := OLD.instance_id;
    ELSE
        v_instance_id := NEW.instance_id;
    END IF;

    SELECT ci.character_id
    INTO v_character_id
    FROM aios.character_instance ci
    WHERE ci.instance_id=v_instance_id;

    IF v_character_id IS NOT NULL THEN
        v_scope_key := 'char:' || v_character_id;

        INSERT INTO aios.semantic_scope_projection_state (
            scope_key, scope_kind,
            dirty_version, projected_version,
            status, dirty_at, first_dirty_at, updated_at
        )
        VALUES (
            v_scope_key, 'character',
            1, 0,
            'dirty', now(), now(), now()
        )
        ON CONFLICT (scope_key) DO UPDATE
        SET scope_kind='character',
            dirty_version=aios.semantic_scope_projection_state.dirty_version + 1,
            status='dirty',
            dirty_at=now(),
            first_dirty_at=CASE WHEN aios.semantic_scope_projection_state.dirty_version > aios.semantic_scope_projection_state.projected_version THEN COALESCE(aios.semantic_scope_projection_state.first_dirty_at, now()) ELSE now() END,
            last_error=NULL,
            updated_at=now();
    END IF;

    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
