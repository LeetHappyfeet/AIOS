BEGIN;

-- The terminal copy is kept so an old source cannot revive it on the next HUD.
CREATE UNIQUE INDEX IF NOT EXISTS uq_character_goal_inherited_root
  ON aios.character_agent_goal(instance_id,(meta->>'inherited_from_goal_id'))
  WHERE meta ? 'inherited_from_goal_id';

CREATE INDEX IF NOT EXISTS idx_character_goal_persistent
  ON aios.character_agent_goal(instance_id,created_at DESC)
  WHERE meta->>'horizon'='persistent';

ALTER TABLE aios.character_cognitive_opportunity
  DROP CONSTRAINT IF EXISTS character_cognitive_opportunity_opportunity_type_check;
ALTER TABLE aios.character_cognitive_opportunity
  ADD CONSTRAINT character_cognitive_opportunity_opportunity_type_check
  CHECK (opportunity_type IN
    ('memory_recall','knowledge_gap','goal_review','goal_formation','reflection','immediate'));

COMMIT;
