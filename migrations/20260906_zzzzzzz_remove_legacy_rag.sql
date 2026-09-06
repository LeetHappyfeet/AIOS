-- Remove schema artifacts owned only by the retired monolithic RAG subsystem.
-- Qdrant data is external and is intentionally not deleted by a PostgreSQL migration.

DROP TABLE IF EXISTS aios.claim_world_affinity CASCADE;
DROP TABLE IF EXISTS aios.section_cluster_assignment CASCADE;
DROP TABLE IF EXISTS aios.world_split_candidate CASCADE;
DROP TABLE IF EXISTS aios.vector_index_state CASCADE;
