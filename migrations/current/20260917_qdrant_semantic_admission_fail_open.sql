-- Extend Qdrant semantic admission with terminal fail-open decisions.
-- New migration because applied migration contents are immutable.

BEGIN;

ALTER TABLE aios.semantic_neighbor_admission
    DROP CONSTRAINT IF EXISTS semantic_neighbor_admission_decision_check;

ALTER TABLE aios.semantic_neighbor_admission
    ADD CONSTRAINT semantic_neighbor_admission_decision_check
    CHECK (decision IN (
        'reinforces',
        'refines',
        'challenges',
        'novel',
        'bypass_unavailable',
        'bypass_timeout',
        'bypass_error'
    ));

COMMENT ON TABLE aios.semantic_neighbor_admission IS
'Qdrant candidate generation plus PostgreSQL semantic verification. Reinforcements are evidence-only; all other terminal decisions expand. Vector failures fail open so admission cannot strand memory.';

COMMIT;
