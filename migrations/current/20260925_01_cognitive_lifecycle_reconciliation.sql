BEGIN;

ALTER TABLE aios.character_cognitive_thread
  ADD COLUMN IF NOT EXISTS goal_id uuid
  REFERENCES aios.character_agent_goal(goal_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_cognitive_thread_goal
  ON aios.character_cognitive_thread(goal_id,status,last_crossed_at DESC);

CREATE TABLE IF NOT EXISTS aios.character_goal_evidence (
  goal_evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  goal_id uuid NOT NULL REFERENCES aios.character_agent_goal(goal_id) ON DELETE CASCADE,
  instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
  evidence_type text NOT NULL,
  relation text NOT NULL CHECK (relation IN
    ('progress','completion_candidate','blocker','contradiction','withdrawal')),
  evidence_id text,
  source_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  confidence double precision NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_character_goal_evidence_identity
  ON aios.character_goal_evidence(
    goal_id,evidence_type,COALESCE(evidence_id,''),
    relation,COALESCE(source_node_id,'00000000-0000-0000-0000-000000000000'::uuid)
  );

CREATE INDEX IF NOT EXISTS idx_character_goal_evidence_goal
  ON aios.character_goal_evidence(goal_id,created_at DESC);

COMMIT;
