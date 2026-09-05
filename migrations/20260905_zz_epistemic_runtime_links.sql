-- Link epistemic character knowledge to concrete runtime entities.
-- Deliberately sorts after 20260905_world_runtime.sql.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='knowledge_acquisition_source_entity_fkey'
          AND conrelid='aios.knowledge_acquisition_event'::regclass
    ) THEN
        ALTER TABLE aios.knowledge_acquisition_event
            ADD CONSTRAINT knowledge_acquisition_source_entity_fkey
            FOREIGN KEY (source_entity_id)
            REFERENCES aios.world_entity(entity_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='character_proposition_knowledge_source_entity_fkey'
          AND conrelid='aios.character_proposition_knowledge'::regclass
    ) THEN
        ALTER TABLE aios.character_proposition_knowledge
            ADD CONSTRAINT character_proposition_knowledge_source_entity_fkey
            FOREIGN KEY (source_entity_id)
            REFERENCES aios.world_entity(entity_id);
    END IF;
END $$;

COMMIT;
