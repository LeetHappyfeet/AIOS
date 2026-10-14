-- Read-only, revision-bound inquiry receipts. No FK to managed goals or acquisitions.
BEGIN;
CREATE TABLE IF NOT EXISTS aios.character_inquiry (
  inquiry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
  source_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
  demand_fingerprint text NOT NULL,
  origin text NOT NULL,
  uncertainty_kind text NOT NULL,
  evidence_scope text NOT NULL CHECK (evidence_scope IN ('source_local','character_accessible')),
  evidence_revision text NOT NULL,
  policy_version text NOT NULL,
  demand jsonb NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK
     (status IN ('queued','planning','resolved','partial','unresolved','conflicting','failed')),
  result jsonb,
  model_calls integer NOT NULL DEFAULT 0 CHECK(model_calls BETWEEN 0 AND 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(instance_id,demand_fingerprint)
);
CREATE INDEX IF NOT EXISTS character_inquiry_instance_idx
 ON aios.character_inquiry(instance_id,created_at DESC);
COMMENT ON TABLE aios.character_inquiry IS
 'Read-only retrieval audit; never a source admission, corpus acquisition, or character-owned claim.';
COMMIT;
