-- Knowledge Atlas stabilization: idempotent host-verified chat research requests.
BEGIN;
CREATE TABLE IF NOT EXISTS aios.character_research_tool_request (
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    source_node_id uuid NOT NULL REFERENCES aios.dag_node(node_id) ON DELETE CASCADE,
    request_hash text NOT NULL,
    question text NOT NULL,
    dossier_id uuid REFERENCES aios.character_research_dossier(dossier_id) ON DELETE SET NULL,
    research_id uuid REFERENCES aios.character_research_event(research_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'claimed' CHECK(status IN ('claimed','completed','failed')),
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(instance_id,source_node_id)
);
COMMENT ON TABLE aios.character_research_tool_request IS
'One host-verified request per character source DAG node. A tool intention is not a corpus acquisition.';
COMMIT;
