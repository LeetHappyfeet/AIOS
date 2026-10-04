-- Source-grounded executive admission and auditable goal-linked research.
-- Neither relation is semantic integrity, belief admission, or goal completion.
BEGIN;
CREATE TABLE IF NOT EXISTS aios.character_goal_admission_receipt (
    receipt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    source_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    source_unit_id uuid REFERENCES aios.message_cognitive_unit(unit_id) ON DELETE SET NULL,
    fingerprint text NOT NULL,
    objective text NOT NULL,
    source_excerpt text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('eligible','deferred','rejected')),
    reason text NOT NULL,
    policy_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(instance_id,fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_goal_admission_source
    ON aios.character_goal_admission_receipt(instance_id,source_node_id,created_at DESC);
ALTER TABLE aios.character_inquiry
    ADD COLUMN IF NOT EXISTS goal_id uuid
    REFERENCES aios.character_agent_goal(goal_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_character_inquiry_goal
    ON aios.character_inquiry(instance_id,goal_id,updated_at DESC)
    WHERE goal_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS aios.character_goal_research_link (
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    goal_id uuid NOT NULL REFERENCES aios.character_agent_goal(goal_id) ON DELETE CASCADE,
    dossier_id uuid NOT NULL REFERENCES aios.character_research_dossier(dossier_id) ON DELETE CASCADE,
    linked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (goal_id,dossier_id)
);
CREATE INDEX IF NOT EXISTS idx_goal_research_instance
    ON aios.character_goal_research_link(instance_id,goal_id,linked_at DESC);
COMMENT ON TABLE aios.character_goal_admission_receipt IS
 'Source-objective eligibility; not semantic integrity or a claim that a goal is fulfilled.';
COMMENT ON TABLE aios.character_goal_research_link IS
 'Provenance link between an admitted owned goal and a bounded research dossier; study remains explicit.';
COMMIT;
