-- Claim-level evidence backing proposition-level reality membership.
--
-- A proposition can be observed by many independent sources. Membership is a
-- materialized aggregate and must not lose one source when another claim is
-- re-evaluated or superseded.
--
-- This migration intentionally sorts after 20260913_reality_hypothesis_layer.sql,
-- which creates aios.reality_context.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.proposition_reality_evidence (
    claim_id uuid NOT NULL REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    reality_context_id uuid NOT NULL REFERENCES aios.reality_context(reality_context_id) ON DELETE CASCADE,
    membership_status text NOT NULL DEFAULT 'candidate',
    affinity double precision NOT NULL DEFAULT 0.5,
    confidence double precision NOT NULL DEFAULT 0.5,
    assigned_by text NOT NULL DEFAULT 'reality-resolver-v1',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (claim_id, reality_context_id),
    CHECK (affinity BETWEEN 0.0 AND 1.0),
    CHECK (confidence BETWEEN 0.0 AND 1.0)
);

CREATE INDEX IF NOT EXISTS idx_proposition_reality_evidence_proposition
    ON aios.proposition_reality_evidence(
        proposition_id, reality_context_id, affinity DESC
    );

CREATE INDEX IF NOT EXISTS idx_proposition_reality_evidence_context
    ON aios.proposition_reality_evidence(
        reality_context_id, proposition_id
    );

COMMIT;
