-- Diagnostic inference reviews belong to an occurrence's exact source/frame
-- revision. They NEVER update claim_semantic_integrity, proposition_evidence,
-- character_knowledge, or character_agent_goal.
CREATE TABLE IF NOT EXISTS aios.claim_source_comparison_audit (
    claim_id uuid NOT NULL REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    revision_key text NOT NULL,
    comparison_version text NOT NULL,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    inference_request_id text NOT NULL,
    verdict text NOT NULL CHECK (
        verdict IN ('supported', 'incomplete', 'contradicted', 'ambiguous')
    ),
    audit_json jsonb NOT NULL,
    audited_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, revision_key)
);
CREATE INDEX IF NOT EXISTS claim_source_comparison_audit_status_idx
    ON aios.claim_source_comparison_audit(verdict, audited_at DESC);
