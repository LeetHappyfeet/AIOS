-- Evidence-backed cognitive opportunities feeding disposable worker HUDs.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_cognitive_opportunity (
  opportunity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
  opportunity_type text NOT NULL CHECK (opportunity_type IN
    ('memory_recall','knowledge_gap','goal_review','reflection','immediate')),
  natural_language text NOT NULL,
  operation_type text NOT NULL,
  operation_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_node_id uuid NULL REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  source_timeline_id uuid NULL,
  source_state_version bigint NULL,
  novelty real NOT NULL DEFAULT 0,
  relevance real NOT NULL DEFAULT 0,
  urgency real NOT NULL DEFAULT 0,
  uncertainty real NOT NULL DEFAULT 0,
  goal_affinity real NOT NULL DEFAULT 0,
  memory_affinity real NOT NULL DEFAULT 0,
  knowledge_gap real NOT NULL DEFAULT 0,
  recency real NOT NULL DEFAULT 0,
  priority_score real NOT NULL DEFAULT 0,
  freshness_policy text NOT NULL DEFAULT 'contextual'
    CHECK (freshness_policy IN ('tolerant','contextual','strict')),
  supersession_key text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','offered','selected','executed','suppressed','superseded','expired')),
  valid_until timestamptz NOT NULL DEFAULT (now()+interval '15 minutes'),
  selected_at timestamptz NULL,
  resolved_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cognitive_opportunity_pending
 ON aios.character_cognitive_opportunity(instance_id,status,priority_score DESC,created_at)
 WHERE status='pending';
CREATE UNIQUE INDEX IF NOT EXISTS uq_cognitive_opportunity_live_key
 ON aios.character_cognitive_opportunity(instance_id,supersession_key)
 WHERE status IN ('pending','offered');

ALTER TABLE aios.internal_cognition_transaction
  ADD COLUMN IF NOT EXISTS opportunity_ids uuid[] NOT NULL DEFAULT '{}'::uuid[];

COMMIT;
