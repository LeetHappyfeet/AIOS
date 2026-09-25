BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_cognitive_subject (
    subject_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    canonical_key text NOT NULL,
    subject_type text NOT NULL,
    entity_keys jsonb NOT NULL DEFAULT '[]'::jsonb,
    predicate_key text,
    object_key text,
    topic_key text,
    question_type text,
    question text,
    display_label text NOT NULL,
    confidence double precision NOT NULL DEFAULT 0.5,
    uncertainty double precision NOT NULL DEFAULT 0.5,
    salience double precision NOT NULL DEFAULT 0.0,
    status text NOT NULL DEFAULT 'provisional'
      CHECK (status IN ('provisional','established','resolved','suppressed')),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(instance_id, canonical_key)
);

CREATE TABLE IF NOT EXISTS aios.character_cognitive_subject_evidence (
    subject_evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id uuid NOT NULL REFERENCES aios.character_cognitive_subject(subject_id) ON DELETE CASCADE,
    source_node_id uuid,
    proposition_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    frame_id uuid,
    evidence_kind text NOT NULL,
    strength double precision NOT NULL DEFAULT 0.5,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cognitive_subject_evidence
ON aios.character_cognitive_subject_evidence(
    subject_id,
    COALESCE(source_node_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(proposition_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(frame_id, '00000000-0000-0000-0000-000000000000'::uuid),
    evidence_kind
);

ALTER TABLE aios.character_cognitive_opportunity
  ADD COLUMN IF NOT EXISTS subject_id uuid
  REFERENCES aios.character_cognitive_subject(subject_id) ON DELETE SET NULL;

ALTER TABLE aios.character_cognitive_thread
  ADD COLUMN IF NOT EXISTS subject_id uuid
  REFERENCES aios.character_cognitive_subject(subject_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_cognitive_subject_instance_seen
  ON aios.character_cognitive_subject(instance_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_cognitive_subject_evidence_subject
  ON aios.character_cognitive_subject_evidence(subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cognitive_opportunity_subject
  ON aios.character_cognitive_opportunity(subject_id);
CREATE INDEX IF NOT EXISTS idx_cognitive_thread_subject
  ON aios.character_cognitive_thread(subject_id);

COMMIT;
