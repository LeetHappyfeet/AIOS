BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_cognitive_thread (
    thread_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    subject_key text NOT NULL,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','working','resolved','suppressed')),
    pressure double precision NOT NULL DEFAULT 0,
    current_question text,
    source_timeline_id uuid,
    last_source_node_id uuid,
    last_state_version bigint,
    crossing_count integer NOT NULL DEFAULT 0,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_crossed_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(instance_id, subject_key)
);

CREATE TABLE IF NOT EXISTS aios.character_cognitive_thread_crossing (
    crossing_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id uuid NOT NULL REFERENCES aios.character_cognitive_thread(thread_id) ON DELETE CASCADE,
    opportunity_id uuid REFERENCES aios.character_cognitive_opportunity(opportunity_id) ON DELETE SET NULL,
    source_node_id uuid,
    crossing_type text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    strength double precision NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(thread_id, opportunity_id)
);

CREATE TABLE IF NOT EXISTS aios.character_cognitive_operation (
    operation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id uuid REFERENCES aios.character_cognitive_thread(thread_id) ON DELETE SET NULL,
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    opportunity_id uuid REFERENCES aios.character_cognitive_opportunity(opportunity_id) ON DELETE SET NULL,
    transaction_id uuid REFERENCES aios.internal_cognition_transaction(transaction_id) ON DELETE SET NULL,
    operation_type text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','waiting_inference','succeeded','stale','failed')),
    input jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb,
    source_state_version bigint,
    source_node_id uuid,
    freshness_policy text NOT NULL DEFAULT 'contextual',
    depth integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cognitive_thread_instance_pressure_idx
ON aios.character_cognitive_thread(instance_id,status,pressure DESC,last_crossed_at DESC);
CREATE INDEX IF NOT EXISTS cognitive_operation_instance_status_idx
ON aios.character_cognitive_operation(instance_id,status,created_at);

COMMIT;
