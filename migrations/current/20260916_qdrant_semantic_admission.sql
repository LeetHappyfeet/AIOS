-- Qdrant-assisted semantic admission state
-- 2026-09-16
--
-- Exact identity remains the responsibility of semantic_exact_admission. This
-- table records conservative near-neighbor decisions generated from the
-- canonical proposition vector index and verified against PostgreSQL semantics.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_neighbor_admission (
    claim_id uuid PRIMARY KEY REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    matched_proposition_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    matched_claim_id uuid REFERENCES aios.claim_candidate(claim_id) ON DELETE SET NULL,
    scope_key text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('reinforces','refines','challenges','novel')),
    similarity double precision,
    reason text NOT NULL,
    gate_version text NOT NULL DEFAULT 'qdrant-admission-v1',
    evidence_preserved boolean NOT NULL DEFAULT true,
    decided_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_semantic_neighbor_admission_decision
    ON aios.semantic_neighbor_admission (decision, decided_at);
CREATE INDEX IF NOT EXISTS idx_semantic_neighbor_admission_match
    ON aios.semantic_neighbor_admission (matched_proposition_id, scope_key)
    WHERE matched_proposition_id IS NOT NULL;

COMMENT ON TABLE aios.semantic_neighbor_admission IS
'Qdrant candidate generation plus PostgreSQL semantic verification. Only conservative same-scope reinforces decisions suppress duplicate observation RDF/topology; evidence is always preserved.';

COMMIT;
