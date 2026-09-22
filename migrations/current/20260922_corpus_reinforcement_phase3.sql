-- Reinforcement ledger for deterministic corpus learning.
-- Safe when migration filename ordering places this before corpus_research_phase1.
BEGIN;

DO $$
BEGIN
    IF to_regclass('aios.character_research_event') IS NULL THEN
        RAISE NOTICE 'character_research_event not present yet; deferring reinforcement table to corpus_research_phase1';
        RETURN;
    END IF;

    CREATE TABLE IF NOT EXISTS aios.character_corpus_reinforcement (
        reinforcement_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
        character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
        instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
        section_id uuid NOT NULL REFERENCES aios.corpus_section(section_id) ON DELETE CASCADE,
        research_id uuid REFERENCES aios.character_research_event(research_id) ON DELETE SET NULL,
        signal_kind text NOT NULL
            CHECK (signal_kind IN ('reexposure','focus_overlap','semantic_overlap','explicit_study')),
        strength double precision NOT NULL DEFAULT 1.0
            CHECK (strength >= 0.0 AND strength <= 1.0),
        meta jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_corpus_reinforcement_event
        ON aios.character_corpus_reinforcement (
            instance_id, section_id, research_id, signal_kind
        ) NULLS NOT DISTINCT;

    CREATE INDEX IF NOT EXISTS idx_corpus_reinforcement_character_section
        ON aios.character_corpus_reinforcement (character_id, section_id, created_at DESC);

    COMMENT ON TABLE aios.character_corpus_reinforcement IS
    'Deterministic evidence that a character attended again to previously exposed corpus material. It is not knowledge by itself.';
END
$$;

COMMIT;
