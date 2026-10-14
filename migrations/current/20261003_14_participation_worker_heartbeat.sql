-- Independent shadow evaluator readiness. This table is operational telemetry;
-- it never changes live epistemic/participation policy or comparison receipts.
BEGIN;
CREATE TABLE IF NOT EXISTS aios.character_participation_worker_heartbeat (
    worker_name text PRIMARY KEY,
    worker_id text NOT NULL,
    policy_version text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_error text
);
COMMENT ON TABLE aios.character_participation_worker_heartbeat IS
 'Ephemeral evaluator heartbeat. Experiments require a fresh independent shadow worker; V4 remains comparison-only.';
COMMIT;
