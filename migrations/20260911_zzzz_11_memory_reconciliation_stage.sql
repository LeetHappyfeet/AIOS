-- AIOS semantic memory reconciliation stage.
BEGIN;

-- /world current memory is separate from /char belief state. Raw observations,
-- propositions, conflicts, and acquisitions remain evidence/provenance.
CREATE TABLE IF NOT EXISTS aios.world_memory_state (
    world_id uuid NOT NULL
        REFERENCES aios.world(world_id) ON DELETE CASCADE,
    atom_id uuid NOT NULL
        REFERENCES aios.semantic_atom(atom_id) ON DELETE CASCADE,
    stance text NOT NULL CHECK (stance IN ('positive','negative','unresolved')),
    positive_support double precision NOT NULL DEFAULT 0.0
        CHECK (positive_support BETWEEN 0 AND 1),
    negative_support double precision NOT NULL DEFAULT 0.0
        CHECK (negative_support BETWEEN 0 AND 1),
    state_confidence double precision NOT NULL DEFAULT 0.0
        CHECK (state_confidence BETWEEN 0 AND 1),
    preferred_proposition_id uuid
        REFERENCES aios.proposition(proposition_id) ON DELETE SET NULL,
    evidence_count integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    independent_evidence_count integer NOT NULL DEFAULT 0
        CHECK (independent_evidence_count >= 0),
    resolved_through_node_id uuid
        REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    resolver_version text NOT NULL DEFAULT 'memory-reconciliation-v1',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, atom_id)
);

CREATE INDEX IF NOT EXISTS idx_world_memory_state_world
    ON aios.world_memory_state (world_id, stance, state_confidence DESC);

COMMENT ON TABLE aios.world_memory_state IS
'Convergent current /world semantic memory. It is never populated from character belief evidence; raw evidence remains in observation/proposition tables.';

CREATE TABLE IF NOT EXISTS aios.memory_reconciliation_receipt (
    claim_id uuid PRIMARY KEY
        REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL
        REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    atom_id uuid NOT NULL
        REFERENCES aios.semantic_atom(atom_id) ON DELETE CASCADE,
    path text NOT NULL CHECK (path IN ('char','world','evidence')),
    scope_key text NOT NULL,
    outcome text NOT NULL,
    resolver_version text NOT NULL DEFAULT 'memory-reconciliation-v1',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    reconciled_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_reconciliation_receipt_scope
    ON aios.memory_reconciliation_receipt (path, scope_key, updated_at DESC);

COMMENT ON TABLE aios.memory_reconciliation_receipt IS
'Audit boundary between normalized semantic evidence and durable memory promotion. A claim is routed to exactly one of /char, /world, or evidence-only.';

COMMIT;
