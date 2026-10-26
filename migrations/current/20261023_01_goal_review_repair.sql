-- Diagnostic input snapshot; no goal outcomes or historical evidence are rewritten.
ALTER TABLE aios.character_goal_plan
  ADD COLUMN input_context jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX uq_goal_review_revision
ON aios.character_cognitive_operation(instance_id,(input->>'review_revision'))
WHERE operation_type='planning.review' AND input ? 'review_revision'
  AND status IN ('queued','running','waiting_inference','succeeded');
