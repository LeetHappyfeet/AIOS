-- Research discovery receipts are evidence, not corpus documents or character knowledge.
CREATE TABLE IF NOT EXISTS aios.research_external_nomination (
    nomination_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_id uuid NOT NULL REFERENCES aios.character_research_dossier(dossier_id) ON DELETE CASCADE,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
    binding_id uuid NOT NULL REFERENCES aios.workspace_binding(binding_id),
    run_id uuid NOT NULL REFERENCES aios.workspace_run(run_id),
    evidence_id varchar(24) NOT NULL CHECK(evidence_id ~ '^[0-9a-f]{24}$'),
    source_id varchar(64) NOT NULL,
    kind text NOT NULL CHECK(kind IN ('web_search_result','calibre_book','local_document')),
    title text NOT NULL CHECK(char_length(title) BETWEEN 1 AND 200),
    source_reference jsonb NOT NULL DEFAULT '{}'::jsonb,
    document_sha256 varchar(64) CHECK(document_sha256 IS NULL OR document_sha256 ~ '^[0-9a-f]{64}$'),
    section text,
    rationale text NOT NULL CHECK(char_length(rationale) BETWEEN 1 AND 600),
    source_retrieved_at timestamptz,
    status text NOT NULL DEFAULT 'nominated' CHECK(status IN ('nominated','deferred','rejected')),
    payload_sha256 varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,evidence_id)
);
CREATE INDEX IF NOT EXISTS research_external_nomination_dossier_idx
    ON aios.research_external_nomination(dossier_id,created_at DESC);
COMMENT ON TABLE aios.research_external_nomination IS
    'Worker-attested retrieval nominations. Never implies corpus import, source authenticity, knowledge acquisition, or fact admission.';
