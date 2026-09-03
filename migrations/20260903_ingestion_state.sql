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

-- The old UNIQUE(world_id, name) forced every liminal session onto one shared
-- timeline. That interleaves unrelated conversations and web sources in the
-- temporal DAG. Timeline identity must include the conversation/source scope.
ALTER TABLE ONLY aios.timeline
    DROP CONSTRAINT IF EXISTS timeline_world_id_name_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_timeline_identity
    ON aios.timeline (
        world_id,
        name,
        session_id,
        character_id,
        user_name,
        scope_key
    ) NULLS NOT DISTINCT;

-- rdf_promotion_log records multiple RDF actions for a claim (for example the
-- base rdf:type promotion and later world:contentKind classification). The
-- stable receipt identity is therefore claim + dataset + graph + predicate.
-- Remove historical duplicates of the SAME action before enforcing it.
WITH ranked_receipts AS (
    SELECT
        promotion_id,
        row_number() OVER (
            PARTITION BY claim_id, rdf_dataset, rdf_graph, rdf_predicate
            ORDER BY promoted_at, promotion_id
        ) AS rn
    FROM aios.rdf_promotion_log
)
DELETE FROM aios.rdf_promotion_log rpl
USING ranked_receipts rr
WHERE rpl.promotion_id = rr.promotion_id
  AND rr.rn > 1;

DROP INDEX IF EXISTS aios.ux_rdf_promotion_claim_dataset_graph;

CREATE UNIQUE INDEX IF NOT EXISTS ux_rdf_promotion_claim_graph_predicate
    ON aios.rdf_promotion_log (
        claim_id,
        rdf_dataset,
        rdf_graph,
        rdf_predicate
    );

-- -------------------------------------------------
-- Reconstruct stage latches for existing data
-- -------------------------------------------------
-- Old versions marked ingest_event.done immediately after DAG insertion. The
-- following updates derive the new stage state from durable projections and
-- RDF receipts already present in SQL.

UPDATE aios.ingest_event ie
SET dag_processed_at = COALESCE(ie.dag_processed_at, n.created_at)
FROM aios.dag_node n
WHERE n.event_id = ie.event_id;

UPDATE aios.ingest_event ie
SET section_processed_at = COALESCE(
        ie.section_processed_at,
        ds.claims_extracted_at,
        n.created_at
    )
FROM aios.dag_node n
JOIN aios.document_section ds
  ON ds.node_id = n.node_id
WHERE n.event_id = ie.event_id;

UPDATE aios.ingest_event ie
SET claims_processed_at = COALESCE(ie.claims_processed_at, ds.claims_extracted_at)
FROM aios.dag_node n
JOIN aios.document_section ds
  ON ds.node_id = n.node_id
WHERE n.event_id = ie.event_id
  AND ds.claims_extracted_at IS NOT NULL;

-- A section is RDF-complete only when every claim has the base
-- rdf:type/world:Claim promotion receipt. A zero-claim section is complete as
-- soon as claim extraction has terminated because there is nothing to emit.
UPDATE aios.ingest_event ie
SET rdf_processed_at = COALESCE(
        ie.rdf_processed_at,
        (
            SELECT max(rpl.promoted_at)
            FROM aios.extracted_sentence es
            JOIN aios.claim_candidate cc
              ON cc.sentence_id = es.sentence_id
            JOIN aios.rdf_promotion_log rpl
              ON rpl.claim_id = cc.claim_id
             AND rpl.rdf_dataset = 'world'
             AND rpl.rdf_graph = 'urn:aios:world:liminal'
             AND rpl.rdf_predicate = 'rdf:type'
             AND rpl.rdf_object = 'world:Claim'
            WHERE es.section_id = ds.section_id
        ),
        ds.claims_extracted_at,
        now()
    ),
    process_status = 'done',
    processed_at = COALESCE(ie.processed_at, now()),
    process_error = NULL,
    rdf_error = NULL
FROM aios.dag_node n
JOIN aios.document_section ds
  ON ds.node_id = n.node_id
WHERE n.event_id = ie.event_id
  AND ds.claims_extracted_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM aios.extracted_sentence es
      JOIN aios.claim_candidate cc
        ON cc.sentence_id = es.sentence_id
      WHERE es.section_id = ds.section_id
        AND NOT EXISTS (
            SELECT 1
            FROM aios.rdf_promotion_log rpl
            WHERE rpl.claim_id = cc.claim_id
              AND rpl.rdf_dataset = 'world'
              AND rpl.rdf_graph = 'urn:aios:world:liminal'
              AND rpl.rdf_predicate = 'rdf:type'
              AND rpl.rdf_object = 'world:Claim'
        )
  );

-- Normalize the old meaning of process_status='done'. Non-document events with
-- a DAG anchor but without RDF completion are now explicitly in processing.
UPDATE aios.ingest_event
SET process_status = 'processing',
    processed_at = NULL
WHERE kind <> 'document'
  AND dag_processed_at IS NOT NULL
  AND rdf_processed_at IS NULL;

-- Metadata-only document root events have no direct RDF projection. Their
-- paragraph child events carry the actual content through the full pipeline.
UPDATE aios.ingest_event
SET process_status = 'done',
    processed_at = COALESCE(processed_at, dag_processed_at, now())
WHERE kind = 'document'
  AND dag_processed_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dag_node_timeline_event
    ON aios.dag_node (timeline_id, event_id DESC);

CREATE INDEX IF NOT EXISTS idx_ingest_event_pipeline_state
    ON aios.ingest_event (process_status, rdf_processed_at, created_at DESC);

COMMIT;
