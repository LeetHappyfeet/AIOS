-- AIOS domain migration: ingestion and provenance
-- Consolidates historical ingestion-state, claim-context, speaker/viewpoint,
-- external-source provenance, and source-event supersession migrations.
-- Historical source migrations are retained under migrations/legacy.

BEGIN;

-- AIOS ingestion state-machine migration
-- 2026-09-03
--
-- Keeps the existing relational model intact while adding explicit stage
-- latches for end-to-end ingestion and a source-time anchor on DAG nodes.


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

-- -------------------------------------------------

-- AIOS claim context resolution layer
-- 2026-09-06
--
-- Adds durable, source-lineage-derived semantic context between liminal
-- structural classification and normalized proposition creation. Presence in
-- this table describes where an assertion came from; it does not make the
-- assertion true in a world or known by any other character.


CREATE TABLE IF NOT EXISTS aios.claim_context_resolution (
    claim_id uuid PRIMARY KEY
        REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,

    claim_kind text NOT NULL DEFAULT 'UNKNOWN',
    subject_kind text,
    object_kind text,
    predicate_family text NOT NULL DEFAULT 'UNKNOWN',

    -- Provenance identity is intentionally not an FK: ingest can observe a
    -- character_id before character discovery/registry materialization.
    origin_character_id text,
    character_instance_id uuid
        REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL,
    viewpoint_id text,
    world_id uuid REFERENCES aios.world(world_id) ON DELETE SET NULL,
    timeline_id uuid REFERENCES aios.timeline(timeline_id) ON DELETE SET NULL,
    dag_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,

    epistemic_scope text NOT NULL DEFAULT 'source',
    acquisition_mode text,

    subject_is_pivot boolean NOT NULL DEFAULT false,
    object_is_pivot boolean NOT NULL DEFAULT false,

    confidence double precision NOT NULL DEFAULT 0.0
        CHECK (confidence BETWEEN 0.0 AND 1.0),
    resolver_version text NOT NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claim_context_character
    ON aios.claim_context_resolution (origin_character_id, resolved_at DESC)
    WHERE origin_character_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_context_instance
    ON aios.claim_context_resolution (character_instance_id, resolved_at DESC)
    WHERE character_instance_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_context_world
    ON aios.claim_context_resolution (world_id, resolved_at DESC)
    WHERE world_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_context_semantics
    ON aios.claim_context_resolution (claim_kind, predicate_family);

-- -------------------------------------------------

-- Character HUD profiles and durable identity context
-- 2026-09-06

ALTER TABLE aios.ingest_event
    ADD COLUMN IF NOT EXISTS viewpoint_id text;

ALTER TABLE aios.dag_node
    ADD COLUMN IF NOT EXISTS viewpoint_id text;

ALTER TABLE aios.claim_context_resolution
    ADD COLUMN IF NOT EXISTS speaker_id text,
    ADD COLUMN IF NOT EXISTS speaker_type text;

-- Preserve already-ingested explicit viewpoints where they were carried only
-- in JSON payload. Otherwise resolve deterministic defaults once.
UPDATE aios.ingest_event
SET viewpoint_id = COALESCE(
    NULLIF(payload->>'viewpoint_id',''),
    CASE
        WHEN speaker_role::text = 'character' THEN COALESCE(speaker_id, character_id)
        ELSE speaker_id
    END
)
WHERE viewpoint_id IS NULL;

UPDATE aios.dag_node
SET viewpoint_id = COALESCE(
    NULLIF(payload->>'viewpoint_id',''),
    CASE
        WHEN speaker_role::text = 'character' THEN COALESCE(speaker_id, character_id)
        ELSE speaker_id
    END
)
WHERE viewpoint_id IS NULL;

-- -------------------------------------------------

-- First-class external observation provenance boundaries
-- 2026-09-06
--
-- External sources are observations, not characters and not world truth.
-- This migration gives future accumulators a durable source identity and
-- explicit target hints while preserving liminal-first ingestion.


ALTER TYPE aios.actor_type
    ADD VALUE IF NOT EXISTS 'source';

ALTER TYPE aios.event_kind
    ADD VALUE IF NOT EXISTS 'observation';

CREATE TABLE IF NOT EXISTS aios.source_identity (
    source_id text PRIMARY KEY,
    source_kind text NOT NULL,
    display_name text,
    canonical_uri text,
    canonical_domain text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_identity_domain
    ON aios.source_identity (canonical_domain)
    WHERE canonical_domain IS NOT NULL;

ALTER TABLE aios.ingest_event
    ADD COLUMN IF NOT EXISTS source_id text
        REFERENCES aios.source_identity(source_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source_kind text,
    ADD COLUMN IF NOT EXISTS target_character_id text,
    ADD COLUMN IF NOT EXISTS target_world_id uuid
        REFERENCES aios.world(world_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS provenance_version text
        NOT NULL DEFAULT 'provenance-v1';

ALTER TABLE aios.timeline
    ALTER COLUMN user_name DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS source_id text
        REFERENCES aios.source_identity(source_id) ON DELETE SET NULL;

ALTER TABLE aios.claim_context_resolution
    ADD COLUMN IF NOT EXISTS source_id text
        REFERENCES aios.source_identity(source_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source_kind text,
    ADD COLUMN IF NOT EXISTS target_character_id text,
    ADD COLUMN IF NOT EXISTS target_world_id uuid
        REFERENCES aios.world(world_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_ingest_event_source_identity
    ON aios.ingest_event (source_id, created_at DESC)
    WHERE source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ingest_event_target_character
    ON aios.ingest_event (target_character_id, created_at DESC)
    WHERE target_character_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ingest_event_target_world
    ON aios.ingest_event (target_world_id, created_at DESC)
    WHERE target_world_id IS NOT NULL;

DROP INDEX IF EXISTS aios.ux_timeline_identity;

CREATE UNIQUE INDEX ux_timeline_identity
    ON aios.timeline (
        world_id,
        name,
        session_id,
        character_id,
        user_name,
        scope_key,
        source_id
    ) NULLS NOT DISTINCT;

CREATE INDEX IF NOT EXISTS idx_timeline_source_identity
    ON aios.timeline (source_id, created_at DESC)
    WHERE source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_context_source
    ON aios.claim_context_resolution (source_id, resolved_at DESC)
    WHERE source_id IS NOT NULL;

-- -------------------------------------------------

-- AIOS source-event supersession for editable/swipe-based clients
-- 2026-09-06
--
-- Preserve immutable ingest/DAG provenance while allowing one logical source
-- message slot (for example a SillyTavern swipe) to replace a prior active
-- alternative without treating both alternatives as sequential lived history.


ALTER TABLE aios.ingest_event
    ADD COLUMN IF NOT EXISTS superseded_at timestamptz,
    ADD COLUMN IF NOT EXISTS superseded_by_event_id bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname='ingest_event_superseded_by_event_id_fkey'
          AND conrelid='aios.ingest_event'::regclass
    ) THEN
        ALTER TABLE aios.ingest_event
            ADD CONSTRAINT ingest_event_superseded_by_event_id_fkey
            FOREIGN KEY (superseded_by_event_id)
            REFERENCES aios.ingest_event(event_id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ingest_event_active_source_slot
    ON aios.ingest_event (session_id, source, source_event_id, event_id DESC)
    WHERE superseded_at IS NULL
      AND source_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ingest_event_superseded
    ON aios.ingest_event (superseded_at, event_id)
    WHERE superseded_at IS NOT NULL;

COMMIT;
