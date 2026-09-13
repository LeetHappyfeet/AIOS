-- Language-neutral semantic interpretation layer.
--
-- claim_semantic_frame remains source-language linguistic evidence.
-- semantic_interpretation is the stable bridge from that evidence into the
-- epistemic/proposition system.  Downstream code should reason over semantic
-- type/roles rather than English lemmas whenever an interpretation exists.

CREATE TABLE IF NOT EXISTS aios.semantic_interpretation (
    interpretation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id uuid NOT NULL REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    frame_id uuid NOT NULL UNIQUE REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE CASCADE,
    semantic_type text NOT NULL,
    source_language text NOT NULL DEFAULT 'en',
    standalone_semantic boolean NOT NULL DEFAULT false,
    confidence double precision NOT NULL DEFAULT 0.0,
    interpreter_version text NOT NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    interpreted_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT semantic_interpretation_confidence_check
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT semantic_interpretation_type_check
        CHECK (semantic_type IN (
            'ACTION','CAUSE','COMMUNICATION','DESIRE','DESCRIPTION','IDENTITY',
            'INTENTION','LOCATION','MEMORY','MENTAL_STATE','POSSESSION','RELATION',
            'RULE','TEMPORAL','UNKNOWN'
        ))
);

CREATE INDEX IF NOT EXISTS semantic_interpretation_claim_idx
    ON aios.semantic_interpretation(claim_id);
CREATE INDEX IF NOT EXISTS semantic_interpretation_type_idx
    ON aios.semantic_interpretation(semantic_type, standalone_semantic);

CREATE TABLE IF NOT EXISTS aios.semantic_interpretation_role (
    interpretation_id uuid NOT NULL
        REFERENCES aios.semantic_interpretation(interpretation_id) ON DELETE CASCADE,
    role_name text NOT NULL,
    ordinal integer NOT NULL DEFAULT 0,
    value_text text,
    entity_key text,
    child_frame_id uuid REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE CASCADE,
    confidence double precision NOT NULL DEFAULT 1.0,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (interpretation_id, role_name, ordinal),
    CONSTRAINT semantic_interpretation_role_target_check
        CHECK (value_text IS NOT NULL OR entity_key IS NOT NULL OR child_frame_id IS NOT NULL),
    CONSTRAINT semantic_interpretation_role_confidence_check
        CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE INDEX IF NOT EXISTS semantic_interpretation_role_child_idx
    ON aios.semantic_interpretation_role(child_frame_id)
    WHERE child_frame_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS semantic_interpretation_role_entity_idx
    ON aios.semantic_interpretation_role(entity_key)
    WHERE entity_key IS NOT NULL;

COMMENT ON TABLE aios.semantic_interpretation IS
    'Language-neutral semantic meaning derived from a source-language claim frame; evidence is preserved separately in claim_semantic_frame.';
COMMENT ON COLUMN aios.semantic_interpretation.standalone_semantic IS
    'Whether this interpretation is safe to promote as an independent atomic proposition rather than only nested semantic content.';
COMMENT ON TABLE aios.semantic_interpretation_role IS
    'Typed arguments/content links for a semantic interpretation; child_frame_id expresses nested semantic content without frame:N text leakage.';
