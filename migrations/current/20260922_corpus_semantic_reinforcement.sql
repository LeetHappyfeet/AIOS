-- Extend corpus reinforcement with proposition-structured semantic overlap.
BEGIN;

ALTER TABLE aios.character_corpus_reinforcement
    DROP CONSTRAINT IF EXISTS character_corpus_reinforcement_signal_kind_check;

ALTER TABLE aios.character_corpus_reinforcement
    ADD CONSTRAINT character_corpus_reinforcement_signal_kind_check
    CHECK (signal_kind IN ('reexposure','focus_overlap','semantic_overlap','explicit_study'));

COMMIT;
