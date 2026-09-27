-- Repair goal evidence polluted by cognitive-operation infrastructure failures.
--
-- Before the lifecycle boundary was corrected, any failed or stale cognitive
-- operation linked to a goal was recorded as semantic blocker evidence. An
-- executor/parser/provider failure says nothing about whether the character's
-- goal is blocked, so remove only rows whose evidence_id points at an operation
-- that actually terminated failed/stale.
DELETE FROM aios.character_goal_evidence AS e
USING aios.character_cognitive_operation AS o
WHERE e.evidence_type = 'cognitive_operation'
  AND e.relation = 'blocker'
  AND e.evidence_id = o.operation_id::text
  AND e.instance_id = o.instance_id
  AND o.status IN ('failed', 'stale');
