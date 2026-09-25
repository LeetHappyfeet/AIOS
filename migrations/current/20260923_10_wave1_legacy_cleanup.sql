-- Wave 1 legacy schema cleanup.
-- These objects have no active runtime consumers. The baseline remains immutable;
-- fresh databases create them from the historical baseline and remove them here.

BEGIN;

ALTER TABLE IF EXISTS aios.character_instance
    DROP CONSTRAINT IF EXISTS character_instance_character_id_world_id_owner_user_id_key;
ALTER TABLE IF EXISTS aios.character_instance
    DROP COLUMN IF EXISTS owner_user_id;

ALTER TABLE IF EXISTS aios.world_lineage
    DROP CONSTRAINT IF EXISTS world_lineage_split_id_fkey;
ALTER TABLE IF EXISTS aios.world_lineage
    DROP COLUMN IF EXISTS split_id;

DROP TABLE IF EXISTS aios.memory_item;
DROP TABLE IF EXISTS aios.pipeline_stage_config;
DROP TABLE IF EXISTS aios.claim_similarity_edge;
DROP TABLE IF EXISTS aios.claim_contradiction_candidate;
DROP TABLE IF EXISTS aios.world_split_pressure;
DROP TABLE IF EXISTS aios.world_split_candidate_world;
DROP TABLE IF EXISTS aios.user_identity;

COMMIT;
