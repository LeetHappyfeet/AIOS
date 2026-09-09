-- AIOS semantic frame decomposition
-- 2026-09-09
--
-- Raw claim_candidate rows remain immutable linguistic evidence. This domain
-- layer stores versioned semantic interpretations, including nested clauses
-- and contextual referent resolution, before context classification and
-- proposition normalization.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.claim_semantic_frame (
    frame_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    claim_id uuid NOT NULL REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    frame_index integer NOT NULL,
    parent_frame_id uuid REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE SET NULL,
    object_frame_id uuid REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE SET NULL,

    subject_text text,
    predicate_surface text,
    predicate_canonical text,
    object_text text,

    resolved_subject text,
    resolved_object text,
    subject_entity_key text,
    object_entity_key text,
    subject_kind_guess text,
    object_kind_guess text,

    polarity smallint NOT NULL DEFAULT 1 CHECK (polarity IN (-1, 1)),
    modality text NOT NULL DEFAULT 'asserted',
    tense text,
    aspect text,
    discourse_mode text NOT NULL DEFAULT 'narrated_observation',
    frame_role text NOT NULL DEFAULT 'clause',

    resolution_status text NOT NULL DEFAULT 'unresolved'
        CHECK (resolution_status IN ('unresolved','partial','resolved','ambiguous')),

    extraction_confidence double precision NOT NULL DEFAULT 0.0
        CHECK (extraction_confidence >= 0.0 AND extraction_confidence <= 1.0),
    predicate_confidence double precision NOT NULL DEFAULT 0.0
        CHECK (predicate_confidence >= 0.0 AND predicate_confidence <= 1.0),
    entity_confidence double precision NOT NULL DEFAULT 0.0
        CHECK (entity_confidence >= 0.0 AND entity_confidence <= 1.0),
    referent_confidence double precision NOT NULL DEFAULT 0.0
        CHECK (referent_confidence >= 0.0 AND referent_confidence <= 1.0),
    frame_confidence double precision NOT NULL DEFAULT 0.0
        CHECK (frame_confidence >= 0.0 AND frame_confidence <= 1.0),

    canonical_text text,
    decomposer_version text NOT NULL,
    resolver_version text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,

    UNIQUE (claim_id, frame_index, decomposer_version)
);

CREATE INDEX IF NOT EXISTS idx_claim_semantic_frame_claim
    ON aios.claim_semantic_frame (claim_id, frame_index);

CREATE INDEX IF NOT EXISTS idx_claim_semantic_frame_subject_entity
    ON aios.claim_semantic_frame (subject_entity_key)
    WHERE subject_entity_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_semantic_frame_object_entity
    ON aios.claim_semantic_frame (object_entity_key)
    WHERE object_entity_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_claim_semantic_frame_canonical
    ON aios.claim_semantic_frame (claim_id, predicate_canonical);

CREATE TABLE IF NOT EXISTS aios.claim_semantic_frame_projection (
    claim_id uuid PRIMARY KEY REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    primary_frame_id uuid REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE SET NULL,
    decomposer_version text NOT NULL,
    resolver_version text,
    frame_count integer NOT NULL DEFAULT 0,
    resolved_count integer NOT NULL DEFAULT 0,
    projected_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMIT;
