BEGIN;

CREATE TABLE IF NOT EXISTS aios.retrieval_trace (
    trace_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    timeline_id uuid NULL,
    head_node_id uuid NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS retrieval_trace_character_created_idx
    ON aios.retrieval_trace(character_id, created_at DESC);

CREATE TABLE IF NOT EXISTS aios.retrieval_trace_item (
    trace_id uuid NOT NULL REFERENCES aios.retrieval_trace(trace_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    source_scope_key text NOT NULL,
    retrieval_reason text NOT NULL,
    rank integer NOT NULL,
    relevance_score double precision NULL,
    injected boolean NOT NULL DEFAULT false,
    PRIMARY KEY (trace_id, proposition_id, source_scope_key)
);

CREATE INDEX IF NOT EXISTS retrieval_trace_item_proposition_idx
    ON aios.retrieval_trace_item(proposition_id);

CREATE TABLE IF NOT EXISTS aios.character_retrieval_affinity (
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition(proposition_id) ON DELETE CASCADE,
    hit_count bigint NOT NULL DEFAULT 0,
    use_count bigint NOT NULL DEFAULT 0,
    affinity double precision NOT NULL DEFAULT 0.0,
    last_retrieved_at timestamptz NULL,
    last_used_at timestamptz NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (character_id, proposition_id)
);

COMMIT;
