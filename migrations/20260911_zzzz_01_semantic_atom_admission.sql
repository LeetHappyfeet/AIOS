-- AIOS character belief reconciliation phase 1.
BEGIN;

-- Character belief reconciliation layer.
--
-- Evidence topology remains append-only provenance: observations, acquisitions,
-- propositions, source/world anchors, and semantic relationships are preserved.
-- This migration adds a separate convergent /char belief state so active
-- cognition is not inferred directly from raw acquired evidence.


-- ---------------------------------------------------------------------------
-- Polarity-independent semantic identity.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aios.semantic_atom (
    atom_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    atom_key text NOT NULL UNIQUE,
    subject_norm text,
    predicate_norm text,
    object_norm text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE aios.proposition
    ADD COLUMN IF NOT EXISTS atom_id uuid REFERENCES aios.semantic_atom(atom_id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_proposition_atom
    ON aios.proposition (atom_id);

CREATE OR REPLACE FUNCTION aios.assign_semantic_atom()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_atom_key text;
    v_atom_id uuid;
BEGIN
    v_atom_key := CASE
        WHEN NEW.subject_norm IS NULL
         AND NEW.predicate_norm IS NULL
         AND NEW.object_norm IS NULL
            THEN 'raw:' || COALESCE(NEW.canonical_text, '')
        ELSE COALESCE(NEW.subject_norm, '') || E'\x1f'
           || COALESCE(NEW.predicate_norm, '') || E'\x1f'
           || COALESCE(NEW.object_norm, '')
    END;

    INSERT INTO aios.semantic_atom (
        atom_key, subject_norm, predicate_norm, object_norm
    )
    VALUES (
        v_atom_key, NEW.subject_norm, NEW.predicate_norm, NEW.object_norm
    )
    ON CONFLICT (atom_key) DO UPDATE
    SET updated_at=now()
    RETURNING atom_id INTO v_atom_id;

    NEW.atom_id := v_atom_id;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_assign_semantic_atom ON aios.proposition;
CREATE TRIGGER trg_assign_semantic_atom
BEFORE INSERT OR UPDATE OF subject_norm, predicate_norm, object_norm, canonical_text
ON aios.proposition
FOR EACH ROW
EXECUTE FUNCTION aios.assign_semantic_atom();

INSERT INTO aios.semantic_atom (
    atom_key, subject_norm, predicate_norm, object_norm
)
SELECT DISTINCT
    CASE
        WHEN p.subject_norm IS NULL
         AND p.predicate_norm IS NULL
         AND p.object_norm IS NULL
            THEN 'raw:' || COALESCE(p.canonical_text, '')
        ELSE COALESCE(p.subject_norm, '') || E'\x1f'
           || COALESCE(p.predicate_norm, '') || E'\x1f'
           || COALESCE(p.object_norm, '')
    END,
    p.subject_norm,
    p.predicate_norm,
    p.object_norm
FROM aios.proposition p
ON CONFLICT (atom_key) DO NOTHING;

UPDATE aios.proposition p
SET atom_id=a.atom_id
FROM aios.semantic_atom a
WHERE p.atom_id IS NULL
  AND a.atom_key = CASE
        WHEN p.subject_norm IS NULL
         AND p.predicate_norm IS NULL
         AND p.object_norm IS NULL
            THEN 'raw:' || COALESCE(p.canonical_text, '')
        ELSE COALESCE(p.subject_norm, '') || E'\x1f'
           || COALESCE(p.predicate_norm, '') || E'\x1f'
           || COALESCE(p.object_norm, '')
      END;

ALTER TABLE aios.proposition
    ALTER COLUMN atom_id SET NOT NULL;

COMMENT ON TABLE aios.semantic_atom IS
'Polarity-independent semantic question identity. Proposition rows remain evidence assertions about an atom.';

-- ---------------------------------------------------------------------------
-- Explicit semantic evidence admission.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aios.semantic_evidence_admission (
    acquisition_id uuid PRIMARY KEY
        REFERENCES aios.knowledge_acquisition_event(acquisition_id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('active','unresolved','suppressed')),
    reason text NOT NULL,
    confidence double precision NOT NULL DEFAULT 0.0 CHECK (confidence BETWEEN 0 AND 1),
    resolver_version text NOT NULL DEFAULT 'semantic-admission-v1',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_semantic_evidence_admission_status
    ON aios.semantic_evidence_admission (status, updated_at DESC);

COMMENT ON TABLE aios.semantic_evidence_admission IS
'Admission decision for character cognition. Raw evidence is never deleted; unresolved/suppressed evidence remains available for diagnostics and later reinterpretation.';

CREATE OR REPLACE FUNCTION aios.recompute_semantic_evidence_admission(
    p_acquisition_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_claim_id uuid;
    v_raw_text text;
    v_subject_text text;
    v_object_text text;
    v_predicate text;
    v_resolution_status text;
    v_discourse_mode text;
    v_epistemic_scope text;
    v_frame_confidence double precision;
    v_referent_confidence double precision;
    v_context_confidence double precision;
    v_status text;
    v_reason text;
    v_confidence double precision;
    v_ambiguous_quoted_participant boolean := false;
BEGIN
    SELECT kae.claim_id
    INTO v_claim_id
    FROM aios.knowledge_acquisition_event kae
    WHERE kae.acquisition_id=p_acquisition_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF v_claim_id IS NULL THEN
        v_status := 'active';
        v_reason := 'explicit_nonclaim_acquisition';
        v_confidence := 1.0;
    ELSE
        SELECT
            cc.raw_text,
            sf.subject_text,
            sf.object_text,
            sf.predicate_canonical,
            sf.resolution_status,
            sf.discourse_mode,
            sf.frame_confidence,
            sf.referent_confidence,
            ccr.epistemic_scope,
            ccr.confidence
        INTO
            v_raw_text,
            v_subject_text,
            v_object_text,
            v_predicate,
            v_resolution_status,
            v_discourse_mode,
            v_frame_confidence,
            v_referent_confidence,
            v_epistemic_scope,
            v_context_confidence
        FROM aios.claim_candidate cc
        LEFT JOIN aios.claim_semantic_frame_projection sfp
          ON sfp.claim_id=cc.claim_id
        LEFT JOIN aios.claim_semantic_frame sf
          ON sf.frame_id=sfp.primary_frame_id
        LEFT JOIN aios.claim_context_resolution ccr
          ON ccr.claim_id=cc.claim_id
        WHERE cc.claim_id=v_claim_id;

        IF NOT FOUND OR v_predicate IS NULL THEN
            v_status := 'suppressed';
            v_reason := 'missing_semantic_predicate';
            v_confidence := 0.0;
        ELSE
            v_ambiguous_quoted_participant :=
                COALESCE(v_discourse_mode, 'narrated_observation')='narrated_observation'
                AND btrim(COALESCE(v_raw_text, '')) ~ '^["“].*["”][.!?]?$';

            IF v_ambiguous_quoted_participant THEN
                v_status := 'unresolved';
                v_reason := 'quoted_local_discourse_unresolved';
            ELSIF COALESCE(v_resolution_status, 'partial') <> 'resolved' THEN
                v_status := 'unresolved';
                v_reason := 'semantic_frame_partial';
            ELSIF COALESCE(v_referent_confidence, 0.0) < 0.55 THEN
                v_status := 'unresolved';
                v_reason := 'referent_confidence_below_threshold';
            ELSIF COALESCE(v_frame_confidence, 0.0) < 0.55 THEN
                v_status := 'unresolved';
                v_reason := 'frame_confidence_below_threshold';
            ELSE
                v_status := 'active';
                v_reason := 'admitted';
            END IF;

            v_confidence := LEAST(
                1.0,
                GREATEST(
                    0.0,
                    LEAST(
                        COALESCE(v_frame_confidence, 0.0),
                        COALESCE(v_context_confidence, 1.0)
                    )
                )
            );
        END IF;
    END IF;

    INSERT INTO aios.semantic_evidence_admission (
        acquisition_id, status, reason, confidence, resolver_version, meta,
        resolved_at, updated_at
    )
    VALUES (
        p_acquisition_id,
        v_status,
        v_reason,
        v_confidence,
        'semantic-admission-v1',
        jsonb_build_object(
            'claim_id', v_claim_id,
            'epistemic_scope', v_epistemic_scope,
            'discourse_mode', v_discourse_mode,
            'ambiguous_quoted_participant', v_ambiguous_quoted_participant
        ),
        now(),
        now()
    )
    ON CONFLICT (acquisition_id) DO UPDATE
    SET status=EXCLUDED.status,
        reason=EXCLUDED.reason,
        confidence=EXCLUDED.confidence,
        resolver_version=EXCLUDED.resolver_version,
        meta=EXCLUDED.meta,
        resolved_at=now(),
        updated_at=now();
END;
$$;

CREATE OR REPLACE FUNCTION aios.refresh_admission_from_acquisition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM aios.recompute_semantic_evidence_admission(NEW.acquisition_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_semantic_admission_from_acquisition
ON aios.knowledge_acquisition_event;
CREATE TRIGGER trg_refresh_semantic_admission_from_acquisition
AFTER INSERT OR UPDATE OF claim_id, proposition_id, instance_id, confidence, meta
ON aios.knowledge_acquisition_event
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_admission_from_acquisition();

CREATE OR REPLACE FUNCTION aios.refresh_admission_for_claim()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_claim_id uuid;
    rec record;
BEGIN
    IF TG_OP='DELETE' THEN
        v_claim_id := OLD.claim_id;
    ELSE
        v_claim_id := NEW.claim_id;
    END IF;
    FOR rec IN
        SELECT acquisition_id
        FROM aios.knowledge_acquisition_event
        WHERE claim_id=v_claim_id
    LOOP
        PERFORM aios.recompute_semantic_evidence_admission(rec.acquisition_id);
    END LOOP;
    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_admission_from_frame ON aios.claim_semantic_frame;
CREATE TRIGGER trg_refresh_admission_from_frame
AFTER INSERT OR UPDATE OF resolved_subject, resolved_object, predicate_canonical,
    resolution_status, discourse_mode, frame_confidence, referent_confidence
ON aios.claim_semantic_frame
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_admission_for_claim();

DROP TRIGGER IF EXISTS trg_refresh_admission_from_context ON aios.claim_context_resolution;
CREATE TRIGGER trg_refresh_admission_from_context
AFTER INSERT OR UPDATE OF epistemic_scope, confidence, resolver_version
ON aios.claim_context_resolution
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_admission_for_claim();

COMMIT;
