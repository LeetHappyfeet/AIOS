-- Deterministic corpus learning phase 3.
-- This migration is intentionally safe if it sorts before corpus_research_phase1.
-- The canonical exposure table is created by corpus_research_phase1; this file
-- only augments it when it already exists.
BEGIN;

DO $$
BEGIN
    IF to_regclass('aios.character_corpus_exposure') IS NULL THEN
        RAISE NOTICE 'character_corpus_exposure not present yet; deferring learning columns to corpus_research_phase1';
        RETURN;
    END IF;

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
END
$$;

COMMIT;
