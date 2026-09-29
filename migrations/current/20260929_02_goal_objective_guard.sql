BEGIN;

-- An italicized narrative fragment such as "wants to*—as though ..." is
-- incomplete intent, not an objective. Preserve the row for audit but keep
-- it out of the active HUD and goal-review loop.
WITH invalid AS (
  UPDATE aios.character_agent_goal
  SET status='cancelled',completed_at=now(),updated_at=now(),
      meta=meta || '{"resolution_kind":"invalid_goal_objective"}'::jsonb
  WHERE status='active'
    AND meta->>'source'='message_cognition'
    AND COALESCE(meta->>'objective','') ~* '^[[:space:]]*to([[:space:]]*[^[:alpha:][:space:]]|[[:space:]]*$)'
  RETURNING instance_id
), bumped AS (
  UPDATE aios.character_runtime_state
  SET state_version=state_version+1,updated_at=now()
  WHERE instance_id IN (SELECT instance_id FROM invalid)
  RETURNING instance_id
)
UPDATE aios.character_hud_readiness
SET status='dirty',dirty_since=now(),updated_at=now()
WHERE instance_id IN (SELECT instance_id FROM invalid);

COMMIT;
