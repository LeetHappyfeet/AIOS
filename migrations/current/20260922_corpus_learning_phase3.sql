-- Deterministic corpus learning phase 3.
-- Exposure remains distinct from acquisition. Repeated/relevant exposure may
-- become eligible for learning, but only through an explicit policy decision.
BEGIN;

ALTER TABLE aios.character_corpus_exposure
    ADD COLUMN IF NOT EXISTS acquisition_status text NOT NULL DEFAULT 'reference'
        CHECK (acquisition_status IN ('reference','eligible','acquired','rejected')),
    ADD COLUMN IF NOT EXISTS acquisition_score double precision,
    ADD COLUMN IF NOT EXISTS acquisition_reason text,
    ADD COLUMN IF NOT EXISTS consumption_id uuid REFERENCES aios.source_consumption(consumption_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS evaluated_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_character_corpus_exposure_learning
    ON aios.character_corpus_exposure (acquisition_status, exposed_at DESC);

COMMENT ON COLUMN aios.character_corpus_exposure.acquisition_status IS
'Lifecycle of a corpus exposure. reference is temporary; acquired means it crossed the existing source_consumption boundary.';
COMMENT ON COLUMN aios.character_corpus_exposure.acquisition_score IS
'Deterministic learning-policy score. It is not truth confidence.';

COMMIT;
