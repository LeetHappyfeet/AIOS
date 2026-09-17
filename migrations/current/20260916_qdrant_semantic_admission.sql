-- Qdrant-assisted semantic admission state
-- 2026-09-16
--
-- Exact identity remains the responsibility of semantic_exact_admission. This
-- table records conservative near-neighbor decisions generated from the
-- canonical proposition vector index and verified against PostgreSQL semantics.
--
-- Scheduler contract:
--   reinforces                    -> terminal evidence-only; suppress claim RDF/topology
--   refines/challenges/novel      -> terminal expansion; normal downstream work
--   bypass_unavailable/timeout/error -> terminal fail-open expansion
--
-- Failures are deliberately fail-open: vector admission may save work, but it
-- must never become a reason to lose semantic memory when Qdrant/indexing fails.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_neighbor_admission (
    claim_id uuid PRIMARY KEY REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    matched_proposition_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    matched_claim_id uuid REFERENCES aios.claim_candidate(claim_id) ON DELETE SET NULL,
    scope_key text NOT NULL,
    decision text NOT NULL,
    similarity double precision,
    reason text NOT NULL,
    gate_version text NOT NULL DEFAULT 'qdrant-admission-v1',
    evidence_preserved boolean NOT NULL DEFAULT true,
    decided_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Upgrade databases which already applied the first version of this migration.
ALTER TABLE aios.semantic_neighbor_admission
    DROP CONSTRAINT IF EXISTS semantic_neighbor_admission_decision_check;
ALTER TABLE aios.semantic_neighbor_admission
    ADD CONSTRAINT semantic_neighbor_admission_decision_check
    CHECK (decision IN (
        'reinforces','refines','challenges','novel',
        'bypass_unavailable','bypass_timeout','bypass_error'
    ));

CREATE INDEX IF NOT EXISTS idx_semantic_neighbor_admission_decision
    ON aios.semantic_neighbor_admission (decision, decided_at);
CREATE INDEX IF NOT EXISTS idx_semantic_neighbor_admission_match
    ON aios.semantic_neighbor_admission (matched_proposition_id, scope_key)
    WHERE matched_proposition_id IS NOT NULL;

COMMENT ON TABLE aios.semantic_neighbor_admission IS
'Qdrant candidate generation plus PostgreSQL semantic verification. Reinforcements are evidence-only; all other terminal decisions expand. Vector failures fail open so admission cannot strand memory.';

COMMIT;
