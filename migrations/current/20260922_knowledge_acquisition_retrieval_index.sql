-- Keep interactive character retrieval from degenerating into a large semi-join
-- over acquisition provenance. HUD retrieval correlates acquisition rows by
-- (instance_id, proposition_id); without this index PostgreSQL materializes the
-- acquisition/provenance side and can perform millions of join-filter checks.
--
-- Measured on the development dataset: the production-like supersession check
-- fell from ~1508 ms to ~61 ms after adding this index.

CREATE INDEX IF NOT EXISTS idx_knowledge_acquisition_instance_proposition
    ON aios.knowledge_acquisition_event (instance_id, proposition_id);

COMMENT ON INDEX aios.idx_knowledge_acquisition_instance_proposition IS
    'Supports foreground character retrieval and provenance checks keyed by instance and proposition.';
