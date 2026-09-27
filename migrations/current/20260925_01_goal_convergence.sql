-- Converge historical character-owned goal cognition into managed goal state.
-- Ongoing admission is handled transactionally by epistemic/message_cognition.py.
BEGIN;

CREATE INDEX IF NOT EXISTS idx_character_agent_goal_semantic_topic
  ON aios.character_agent_goal(instance_id, ((meta->>'semantic_topic_key')))
  WHERE meta ? 'semantic_topic_key';

WITH latest_topic AS (
  SELECT DISTINCT ON (c.instance_id, u.topic_key)
         c.instance_id,
         c.node_id,
         u.unit_id,
         u.topic_key,
         u.text,
         u.salience,
         u.confidence,
         c.event_id
  FROM aios.message_cognitive_commit c
  JOIN aios.message_cognitive_unit u ON u.commit_id=c.commit_id
  WHERE u.claim_kind='GOAL'
    AND u.status='active'
    AND u.polarity > 0
    AND COALESCE((u.meta->>'character_owned')::boolean, false)=true
  ORDER BY c.instance_id, u.topic_key, c.event_id DESC NULLS LAST, u.created_at DESC
),
ranked AS (
  SELECT *,
         row_number() OVER (
           PARTITION BY instance_id
           ORDER BY event_id DESC NULLS LAST, salience DESC, unit_id DESC
         ) AS recency_rank
  FROM latest_topic
)
INSERT INTO aios.character_agent_goal(
  instance_id, source_node_id, goal_text, status, priority, meta
)
SELECT r.instance_id,
       r.node_id,
       r.text,
       'active',
       GREATEST(10, LEAST(100, round(100 - COALESCE(r.salience,0.0) * 70)::integer)),
       jsonb_build_object(
         'semantic_topic_key', r.topic_key,
         'source', 'message_cognition_backfill',
         'source_unit_id', r.unit_id::text,
         'confidence', r.confidence,
         'salience', r.salience
       )
FROM ranked r
WHERE r.recency_rank <= 3
  AND NOT EXISTS (
    SELECT 1
    FROM aios.character_agent_goal g
    WHERE g.instance_id=r.instance_id
      AND (
        g.meta->>'semantic_topic_key'=r.topic_key
        OR lower(regexp_replace(g.goal_text,'\s+',' ','g'))
           = lower(regexp_replace(r.text,'\s+',' ','g'))
      )
  );

COMMIT;
