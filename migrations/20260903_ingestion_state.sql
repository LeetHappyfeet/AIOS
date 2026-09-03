-- AIOS ingestion state-machine migration
-- 2026-09-03
--
-- Keeps the existing relational model intact while adding explicit stage
-- latches for end-to-end ingestion and a source-time anchor on DAG nodes.

BEGIN;

ALTER TABLE aios.ingest_event
    ADD COLUMN IF NOT EXISTS dag_processed_at timestamptz,
    ADD COLUMN IF NOT EXISTS section_processed_at timestamptz,
    ADD COLUMN IF NOT EXISTS claims_processed_at timestamptz,
    ADD COLUMN IF NOT EXISTS rdf_processed_at timestamptz,
    ADD COLUMN IF NOT EXISTS rdf_error text;

ALTER TABLE aios.dag_node
    ADD COLUMN IF NOT EXISTS event_time timestamptz;

-- Backfill the DAG anchor from the immutable ingest event chronology.
UPDATE aios.dag_node n
SET event_time = COALESCE(e.event_time, e.created_at)
FROM aios.ingest_event e
WHERE e.event_id = n.event_id
  AND n.event_time IS NULL;

-- rdf_promotion_log is a promotion receipt: one successful receipt per
-- claim/dataset/graph. This makes ON CONFLICT meaningful and prevents
-- duplicate SQL acknowledgements of the same RDF promotion.
CREATE UNIQUE INDEX IF NOT EXISTS ux_rdf_promotion_claim_dataset_graph
    ON aios.rdf_promotion_log (claim_id, rdf_dataset, rdf_graph);

CREATE INDEX IF NOT EXISTS idx_dag_node_timeline_event
    ON aios.dag_node (timeline_id, event_id DESC);

CREATE INDEX IF NOT EXISTS idx_ingest_event_pipeline_state
    ON aios.ingest_event (process_status, rdf_processed_at, created_at DESC);

COMMIT;
