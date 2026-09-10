-- AIOS semantic validation decision graph
-- Derived semantic decisions are revisable. New evidence invalidates only the
-- decisions that declared a dependency on it; downstream decisions may depend
-- on earlier decisions through evidence_type='decision'.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_validation_decision (
    decision_type text NOT NULL,
    decision_key text NOT NULL,
    subject_type text NOT NULL,
    subject_key text NOT NULL,
    proposed_value text,
    selected_value text,
    status text NOT NULL CHECK (status IN (
        'verified','protected_explicit','fragile','challenged',
        'insufficient_evidence','single_plausible_owner','stale'
    )),
    resolver_version text NOT NULL,
    winner_score integer NOT NULL DEFAULT 0,
    runner_up_score integer NOT NULL DEFAULT 0,
    margin integer NOT NULL DEFAULT 0,
    stability double precision NOT NULL DEFAULT 0.0 CHECK (stability BETWEEN 0 AND 1),
    matrix jsonb NOT NULL DEFAULT '{}'::jsonb,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision integer NOT NULL DEFAULT 1,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    stale_at timestamptz,
    PRIMARY KEY (decision_type, decision_key)
);

CREATE INDEX IF NOT EXISTS idx_semantic_validation_subject
    ON aios.semantic_validation_decision (subject_type, subject_key);
CREATE INDEX IF NOT EXISTS idx_semantic_validation_stale
    ON aios.semantic_validation_decision (status, stale_at)
    WHERE status='stale';

CREATE TABLE IF NOT EXISTS aios.semantic_validation_dependency (
    decision_type text NOT NULL,
    decision_key text NOT NULL,
    evidence_type text NOT NULL,
    evidence_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (decision_type, decision_key, evidence_type, evidence_key),
    FOREIGN KEY (decision_type, decision_key)
        REFERENCES aios.semantic_validation_decision(decision_type, decision_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_semantic_validation_evidence
    ON aios.semantic_validation_dependency (evidence_type, evidence_key);

COMMENT ON TABLE aios.semantic_validation_decision IS
'Revisable adversarial-matrix decisions. These rows describe derived interpretation, not source truth.';
COMMENT ON TABLE aios.semantic_validation_dependency IS
'Explicit evidence dependency graph used for bounded recursive semantic invalidation when later evidence arrives.';

COMMIT;
