-- Discovery-only bridge: worker-attested sources never imply corpus import or belief.
-- Keep source metadata scoped to the originating character instance and run.
CREATE TABLE IF NOT EXISTS aios.research_external_discovery (
    run_id uuid NOT NULL REFERENCES aios.workspace_run(run_id) ON DELETE CASCADE,
    evidence_id text NOT NULL CHECK (evidence_id ~ '^[0-9a-f]{24}$'),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
    dossier_id uuid REFERENCES aios.character_research_dossier(dossier_id) ON DELETE SET NULL,
    source_id text NOT NULL,
    source_kind text NOT NULL,
    title text NOT NULL,
    document_sha256 text NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
    section text,
    location jsonb,
    public_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id,evidence_id)
);
CREATE INDEX IF NOT EXISTS research_external_discovery_dossier_idx
  ON aios.research_external_discovery(instance_id,dossier_id,created_at DESC);
COMMENT ON TABLE aios.research_external_discovery IS
  'Private worker-attested discovery metadata only. Not a corpus document, acquisition, fact or access grant.';
