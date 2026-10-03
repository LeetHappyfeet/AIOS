BEGIN;
CREATE TABLE IF NOT EXISTS aios.character_participation_experiment (
    experiment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    policy_version text NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','stopped')),
    since_at timestamptz NOT NULL,
    until_at timestamptz NOT NULL,
    max_claims integer NOT NULL CHECK (max_claims BETWEEN 1 AND 1000),
    enqueued_count integer NOT NULL DEFAULT 0 CHECK (enqueued_count BETWEEN 0 AND max_claims),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_participation_experiment_active
 ON aios.character_participation_experiment(instance_id,until_at) WHERE status='running';
CREATE TABLE IF NOT EXISTS aios.character_participation_pending (
    experiment_id uuid NOT NULL REFERENCES aios.character_participation_experiment ON DELETE CASCADE,
    claim_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','evaluated','skipped','failed')),
    queued_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz NOT NULL DEFAULT now(),
    error_count integer NOT NULL DEFAULT 0,
    last_error text,
    PRIMARY KEY (experiment_id,claim_id)
);
CREATE INDEX IF NOT EXISTS idx_participation_pending_ready
 ON aios.character_participation_pending(ready_at,experiment_id,claim_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS aios.character_participation_evaluation (
    experiment_id uuid NOT NULL REFERENCES aios.character_participation_experiment ON DELETE CASCADE,
    claim_id uuid NOT NULL,
    instance_id uuid NOT NULL REFERENCES aios.character_instance ON DELETE CASCADE,
    proposition_id uuid NOT NULL REFERENCES aios.proposition ON DELETE CASCADE,
    policy_version text NOT NULL,
    population text NOT NULL CHECK (population IN ('background','latent','foreground')),
    reasons jsonb NOT NULL,
    signals jsonb NOT NULL,
    claim_snapshot jsonb NOT NULL,
    context_snapshot jsonb NOT NULL,
    context_version text NOT NULL,
    elapsed_ms double precision NOT NULL,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id,claim_id)
);
CREATE INDEX IF NOT EXISTS idx_participation_evaluation_instance
 ON aios.character_participation_evaluation(instance_id,evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_participation_knowledge_recent
 ON aios.character_knowledge(instance_id,updated_at DESC,claim_id);

CREATE INDEX IF NOT EXISTS idx_participation_conflict_a
 ON aios.proposition_conflict(proposition_a_id);
CREATE INDEX IF NOT EXISTS idx_participation_conflict_b
 ON aios.proposition_conflict(proposition_b_id);

-- Enqueue only acquired evidence for opted-in instances. The experiment lock
-- makes the sample cap exact even with concurrent ingestion/backfill.
CREATE OR REPLACE FUNCTION aios.enqueue_shadow_participation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE e record; inserted integer;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM aios.character_participation_experiment
        WHERE instance_id=NEW.instance_id AND status='running' AND until_at>now()
          AND enqueued_count<max_claims) THEN RETURN NEW; END IF;
    FOR e IN SELECT * FROM aios.character_participation_experiment
        WHERE instance_id=NEW.instance_id AND status='running' AND until_at>now()
          AND NEW.updated_at BETWEEN since_at AND until_at AND enqueued_count<max_claims
        ORDER BY experiment_id FOR UPDATE
    LOOP
        INSERT INTO aios.character_participation_pending(experiment_id,claim_id)
        VALUES(e.experiment_id,NEW.claim_id) ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS inserted = ROW_COUNT;
        IF inserted>0 THEN UPDATE aios.character_participation_experiment
            SET enqueued_count=enqueued_count+1 WHERE experiment_id=e.experiment_id; END IF;
    END LOOP;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_shadow_participation_acquisition ON aios.character_knowledge;
CREATE TRIGGER trg_shadow_participation_acquisition
 AFTER INSERT OR UPDATE OF updated_at ON aios.character_knowledge
 FOR EACH ROW EXECUTE FUNCTION aios.enqueue_shadow_participation();
COMMIT;
