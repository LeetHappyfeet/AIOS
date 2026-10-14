BEGIN;

CREATE TABLE aios.character_cognitive_episode (
    episode_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    source_node_id uuid NOT NULL REFERENCES aios.dag_node(node_id),
    source_timeline_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','succeeded','failed')),
    route_transaction_id uuid REFERENCES aios.internal_cognition_transaction(transaction_id),
    sealed_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX cognitive_episode_live_source_idx
    ON aios.character_cognitive_episode(instance_id,source_node_id) WHERE status='active';
CREATE INDEX cognitive_episode_source_history_idx
    ON aios.character_cognitive_episode(instance_id,source_node_id,created_at DESC);
CREATE INDEX cognitive_episode_recovery_idx
    ON aios.character_cognitive_episode(status,updated_at);

ALTER TABLE aios.internal_cognition_transaction
    ADD COLUMN episode_id uuid REFERENCES aios.character_cognitive_episode(episode_id),
    ADD COLUMN reconciled_at timestamptz;
ALTER TABLE aios.character_cognitive_operation
    ADD COLUMN episode_id uuid REFERENCES aios.character_cognitive_episode(episode_id),
    ADD COLUMN episode_role text CHECK (episode_role IN ('attention','maintenance')),
    ADD COLUMN reconciled_at timestamptz;
CREATE UNIQUE INDEX cognitive_operation_episode_opportunity_idx
    ON aios.character_cognitive_operation(episode_id,opportunity_id)
    WHERE episode_id IS NOT NULL AND opportunity_id IS NOT NULL;
CREATE UNIQUE INDEX cognitive_transaction_episode_route_idx
    ON aios.internal_cognition_transaction(episode_id)
    WHERE episode_id IS NOT NULL AND transaction_kind='route';
CREATE INDEX cognitive_operation_episode_status_idx
    ON aios.character_cognitive_operation(episode_id,status);
CREATE INDEX cognitive_operation_transaction_idx
    ON aios.character_cognitive_operation(transaction_id) WHERE transaction_id IS NOT NULL;
CREATE INDEX cognitive_job_active_operation_idx
    ON aios.pipeline_job((payload->>'operation_id')) WHERE status IN ('queued','running');
CREATE INDEX cognitive_operation_unreconciled_idx
    ON aios.character_cognitive_operation(status,updated_at) WHERE reconciled_at IS NULL;
CREATE INDEX cognition_transaction_unreconciled_idx
    ON aios.internal_cognition_transaction(status,expires_at) WHERE reconciled_at IS NULL;

-- Existing terminal receipts predate replayable cleanup. Do not replay all
-- historical goal effects; recovery targets nonterminal legacy rows and every
-- new terminal receipt written after this migration.
UPDATE aios.internal_cognition_transaction SET reconciled_at=COALESCE(completed_at,updated_at)
WHERE status='consumed';
UPDATE aios.character_cognitive_operation SET reconciled_at=COALESCE(completed_at,updated_at)
WHERE status IN ('succeeded','stale','failed');

-- Recover the known historical double encoding; malformed/scalar evidence
-- remains auditable instead of making migration fail or silently discarding it.
DO $$
DECLARE r record; decoded jsonb;
BEGIN
    FOR r IN SELECT crossing_id,evidence FROM aios.character_cognitive_thread_crossing
             WHERE jsonb_typeof(evidence)='string'
    LOOP
        BEGIN
            decoded := (r.evidence #>> '{}')::jsonb;
            IF jsonb_typeof(decoded)='array' THEN
                UPDATE aios.character_cognitive_thread_crossing SET evidence=decoded
                WHERE crossing_id=r.crossing_id;
            END IF;
        EXCEPTION WHEN invalid_text_representation THEN
            CONTINUE;
        END;
    END LOOP;
END $$;

COMMIT;
