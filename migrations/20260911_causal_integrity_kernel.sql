-- AIOS causal integrity kernel
-- 2026-09-11
--
-- Adds the hidden, branch-coherent deterministic state substrate below /world.
-- Semantic/epistemic assertions remain separate and are never promoted merely
-- because they exist.  causal_candidate is proposed reality; causal_admission
-- is the consistency decision; world_event is the immutable committed ledger;
-- causal_state is only the materialized current state for fast validation.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.causal_candidate (
    candidate_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    dag_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    domain_id text NOT NULL,
    event_type text NOT NULL,
    entity_id uuid NOT NULL REFERENCES aios.world_entity(entity_id) ON DELETE CASCADE,
    target_entity_id uuid REFERENCES aios.world_entity(entity_id) ON DELETE SET NULL,
    state_key text NOT NULL,
    value_json jsonb,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_kind text NOT NULL DEFAULT 'semantic',
    source_ref text,
    authority_kind text NOT NULL DEFAULT 'semantic_candidate',
    occurred_at timestamptz,
    claim_id uuid REFERENCES aios.claim_candidate(claim_id) ON DELETE SET NULL,
    frame_id uuid REFERENCES aios.claim_semantic_frame(frame_id) ON DELETE SET NULL,
    proposition_id uuid REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_causal_candidate_coordinate
    ON aios.causal_candidate (world_id, timeline_id, domain_id, entity_id, state_key, created_at);
CREATE INDEX IF NOT EXISTS idx_causal_candidate_claim
    ON aios.causal_candidate (claim_id)
    WHERE claim_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS aios.causal_admission (
    admission_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    candidate_id uuid NOT NULL REFERENCES aios.causal_candidate(candidate_id) ON DELETE CASCADE,
    decision text NOT NULL CHECK (decision IN (
        'ADMITTED',
        'ADMITTED_WITH_LATENT_TRANSITION',
        'REJECTED_IMPOSSIBLE',
        'CONFLICT',
        'UNDERDETERMINED',
        'FORK_REQUIRED',
        'EPISTEMIC_ONLY'
    )),
    reason_code text NOT NULL,
    reason_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    state_version_before bigint NOT NULL DEFAULT 0,
    state_version_after bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_causal_admission_candidate
    ON aios.causal_admission (candidate_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_causal_admission_decision
    ON aios.causal_admission (decision, created_at DESC);

CREATE TABLE IF NOT EXISTS aios.causal_state (
    world_id uuid NOT NULL REFERENCES aios.world(world_id) ON DELETE CASCADE,
    timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    domain_id text NOT NULL,
    entity_id uuid NOT NULL REFERENCES aios.world_entity(entity_id) ON DELETE CASCADE,
    state_key text NOT NULL,
    value_json jsonb,
    state_version bigint NOT NULL DEFAULT 1 CHECK (state_version >= 1),
    last_event_id uuid,
    last_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    occurred_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (world_id, timeline_id, domain_id, entity_id, state_key)
);

CREATE INDEX IF NOT EXISTS idx_causal_state_timeline
    ON aios.causal_state (timeline_id, domain_id, entity_id);

-- Reuse world_event as the canonical deterministic event ledger rather than
-- introducing a competing second event-history table.
ALTER TABLE aios.world_event
    ADD COLUMN IF NOT EXISTS domain_id text,
    ADD COLUMN IF NOT EXISTS source_kind text,
    ADD COLUMN IF NOT EXISTS source_ref text,
    ADD COLUMN IF NOT EXISTS authority_kind text,
    ADD COLUMN IF NOT EXISTS candidate_id uuid REFERENCES aios.causal_candidate(candidate_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS state_key text,
    ADD COLUMN IF NOT EXISTS before_state jsonb,
    ADD COLUMN IF NOT EXISTS delta jsonb,
    ADD COLUMN IF NOT EXISTS result_state jsonb,
    ADD COLUMN IF NOT EXISTS parent_state_version bigint,
    ADD COLUMN IF NOT EXISTS result_state_version bigint,
    ADD COLUMN IF NOT EXISTS occurred_at timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname='causal_state_last_event_fkey'
          AND conrelid='aios.causal_state'::regclass
    ) THEN
        ALTER TABLE aios.causal_state
            ADD CONSTRAINT causal_state_last_event_fkey
            FOREIGN KEY (last_event_id)
            REFERENCES aios.world_event(world_event_id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_world_event_causal_candidate
    ON aios.world_event (candidate_id)
    WHERE candidate_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_world_event_causal_coordinate
    ON aios.world_event (world_id, timeline_id, domain_id, actor_entity_id, occurred_at DESC)
    WHERE domain_id IS NOT NULL;

COMMENT ON TABLE aios.causal_state IS
    'Hidden materialized deterministic state. Never project directly into /char or the HUD.';
COMMENT ON TABLE aios.causal_candidate IS
    'Proposed objective state transition; not world truth until causally admitted.';
COMMENT ON TABLE aios.causal_admission IS
    'Causal consistency decision independent of epistemic confidence.';
COMMENT ON COLUMN aios.character_runtime_state.location_entity_id IS
    'Legacy compatibility field. Canonical location is causal_state world.location and its /world located_in projection.';
COMMENT ON COLUMN aios.character_runtime_state.health IS
    'Legacy compatibility field; deterministic health belongs in a causal domain, not HUD runtime state.';
COMMENT ON COLUMN aios.character_runtime_state.stamina IS
    'Legacy compatibility field; deterministic stamina belongs in a causal domain, not HUD runtime state.';
COMMENT ON COLUMN aios.character_runtime_state.energy IS
    'Legacy compatibility field; deterministic energy belongs in a causal domain, not HUD runtime state.';

COMMIT;
