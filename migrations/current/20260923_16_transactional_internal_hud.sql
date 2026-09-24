-- Disposable, snapshot-bound internal HUD transactions.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.internal_cognition_transaction (
  transaction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
  source_task_id uuid NULL REFERENCES aios.character_cognitive_task(task_id) ON DELETE SET NULL,
  transaction_kind text NOT NULL DEFAULT 'route',
  hud_profile_name text NOT NULL DEFAULT 'agent.transaction.route',
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','proposed','consumed','stale','invalid','superseded','failed')),
  priority integer NOT NULL DEFAULT 150,
  source_state_version bigint NULL,
  source_timeline_id uuid NULL,
  source_node_id uuid NULL REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  context_fingerprint text NOT NULL,
  clues jsonb NOT NULL DEFAULT '[]'::jsonb,
  candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
  prompt_text text NULL,
  prompt_hash text NULL,
  inference_request_id uuid NULL REFERENCES aios.inference_request(request_id) ON DELETE SET NULL,
  choice_key text NULL,
  choice_focus text NULL,
  result jsonb NULL,
  rejection_reason text NULL,
  expires_at timestamptz NOT NULL DEFAULT (now() + interval '5 minutes'),
  claimed_at timestamptz NULL,
  completed_at timestamptz NULL,
  consumed_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_internal_cognition_transaction_pending
 ON aios.internal_cognition_transaction(status,priority,created_at)
 WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_internal_cognition_transaction_instance
 ON aios.internal_cognition_transaction(instance_id,status,created_at);

INSERT INTO aios.hud_profile (
 profile_name,description,token_budget,recent_event_limit,
 memory_budget,belief_budget,relationship_budget,scene_budget,inventory_budget,
 rules_budget,goals_budget,entity_hops,semantic_retrieval_limit,deep_memory_limit,
 include_emotional_state,include_physical_state,include_social_state,
 include_inventory,include_relationships,include_conflicts,include_provenance,
 include_confidence,meta
) VALUES (
 'agent.transaction.route',
 'Disposable micro-HUD for one character-relative internal routing decision.',
 360,2,0,0,0,120,0,0,80,0,0,0,
 true,false,false,false,false,false,false,false,
 '{"purpose":"internal_transaction","surface":"transaction","renderer":"internal-agent-v1","disposable":true}'::jsonb
) ON CONFLICT (profile_name) DO UPDATE SET
 description=EXCLUDED.description,token_budget=EXCLUDED.token_budget,
 recent_event_limit=EXCLUDED.recent_event_limit,scene_budget=EXCLUDED.scene_budget,
 goals_budget=EXCLUDED.goals_budget,meta=EXCLUDED.meta;

COMMIT;
