-- A source occurrence owns semantic integrity, never the shared proposition.
CREATE TABLE IF NOT EXISTS aios.claim_semantic_integrity (
    claim_id uuid PRIMARY KEY REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    revision_key text NOT NULL,
    validator_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('valid','incomplete','invalid')),
    reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_text text NOT NULL,
    frame_snapshot jsonb NOT NULL,
    repair_attempts integer NOT NULL DEFAULT 0,
    repair_state text NOT NULL DEFAULT 'pending',
    checked_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claim_semantic_integrity_status_idx
ON aios.claim_semantic_integrity(status, checked_at);
CREATE TABLE IF NOT EXISTS aios.claim_semantic_integrity_revision (
    claim_id uuid NOT NULL REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    revision_key text NOT NULL,
    validator_version text NOT NULL,
    status text NOT NULL,
    reason_codes jsonb NOT NULL,
    frame_snapshot jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, revision_key, validator_version)
);
