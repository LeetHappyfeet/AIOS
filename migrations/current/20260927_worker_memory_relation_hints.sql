-- Advisory relation judgments emitted by bounded cognitive workers.
-- These are evidence for later validation, never authoritative topology.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_worker_relation_hint (
    hint_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    proposition_a_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    proposition_b_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    judgment text NOT NULL,
    confidence double precision NOT NULL DEFAULT 0.5,
    source_worker text NOT NULL DEFAULT 'research',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT semantic_worker_relation_hint_distinct CHECK (proposition_a_id <> proposition_b_id),
    CONSTRAINT semantic_worker_relation_hint_confidence CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT semantic_worker_relation_hint_judgment CHECK (
        judgment IN ('same_event','contradicts','refines','related','none','uncertain',
                     'garbage_a','garbage_b','garbage_both')
    )
);

CREATE INDEX IF NOT EXISTS idx_semantic_worker_relation_hint_pair
ON aios.semantic_worker_relation_hint (proposition_a_id, proposition_b_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_semantic_worker_relation_hint_instance
ON aios.semantic_worker_relation_hint (instance_id, created_at DESC);

COMMENT ON TABLE aios.semantic_worker_relation_hint IS
'Advisory bounded judgments from cognitive workers. Hints never directly mutate semantic_neighbor_relation, topology, or semantic_evidence_admission. Garbage judgments request deterministic revalidation only.';

COMMIT;
