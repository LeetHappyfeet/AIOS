-- Extend corpus reinforcement with proposition-structured semantic overlap.
-- Safe if the reinforcement table has been deferred until corpus_research_phase1.
BEGIN;

DO $$
BEGIN
    IF to_regclass('aios.character_corpus_reinforcement') IS NULL THEN
        RAISE NOTICE 'character_corpus_reinforcement not present yet; semantic_overlap is included by corpus_research_phase1';
        RETURN;
    END IF;

    ALTER TABLE aios.character_corpus_reinforcement
        DROP CONSTRAINT IF EXISTS character_corpus_reinforcement_signal_kind_check;

    ALTER TABLE aios.character_corpus_reinforcement
        ADD CONSTRAINT character_corpus_reinforcement_signal_kind_check
        CHECK (signal_kind IN ('reexposure','focus_overlap','semantic_overlap','explicit_study'));
END
$$;

COMMIT;
