-- Goal Integration V1 + Goal Research V1. Additive; no epistemic promotion.
BEGIN;
CREATE TABLE IF NOT EXISTS aios.character_goal_knowledge_requirement (
    requirement_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    goal_id uuid NOT NULL REFERENCES aios.character_agent_goal(goal_id) ON DELETE CASCADE,
    requirement_key text NOT NULL,
    question text NOT NULL CHECK (length(btrim(question)) BETWEEN 3 AND 600),
    query_text text NOT NULL CHECK (length(btrim(query_text)) BETWEEN 3 AND 600),
    coverage_status text NOT NULL DEFAULT 'missing'
       CHECK (coverage_status IN ('missing','partial','sufficient','unavailable')),
    coverage_score double precision NOT NULL DEFAULT 0
       CHECK (coverage_score BETWEEN 0 AND 1),
    coverage_source text NOT NULL DEFAULT 'unassessed',
    retrieval_state text NOT NULL DEFAULT 'unassessed',
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instance_id,goal_id,requirement_key),
    UNIQUE (requirement_id,goal_id,instance_id)
);
CREATE INDEX IF NOT EXISTS idx_goal_requirement_active
  ON aios.character_goal_knowledge_requirement(instance_id,goal_id,coverage_status,updated_at DESC);
CREATE TABLE IF NOT EXISTS aios.character_goal_research_requirement_link (
    requirement_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    instance_id uuid NOT NULL,
    dossier_id uuid NOT NULL REFERENCES aios.character_research_dossier(dossier_id) ON DELETE CASCADE,
    linked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (requirement_id,dossier_id),
    FOREIGN KEY (requirement_id,goal_id,instance_id)
       REFERENCES aios.character_goal_knowledge_requirement(requirement_id,goal_id,instance_id)
       ON DELETE CASCADE,
    FOREIGN KEY (goal_id,dossier_id)
       REFERENCES aios.character_goal_research_link(goal_id,dossier_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_goal_requirement_dossier
    ON aios.character_goal_research_requirement_link(instance_id,dossier_id,goal_id);
COMMENT ON TABLE aios.character_goal_knowledge_requirement IS
'One scoped knowledge question required by a managed goal; coverage is retrieval evidence, never a goal completion decision.';
COMMENT ON TABLE aios.character_goal_research_requirement_link IS
'Many-to-many scoped link between requirement, goal and resumable research dossier. No source or belief admission.';
COMMIT;
