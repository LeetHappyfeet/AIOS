-- Parked work remains durable, but must have a non-null eligibility decision.
CREATE OR REPLACE VIEW aios.instance_goal_scheduling AS
SELECT a.*, ar.state AS runtime_state,
       CASE WHEN a.last_interaction_at > now()-interval '15 minutes' THEN 0
            WHEN a.last_interaction_at > now()-interval '1 hour' THEN 1
            WHEN a.background_enabled OR a.explicit_work_until>now() THEN 2 ELSE 3 END AS activity_band,
       COALESCE(ar.state <> 'paused' AND (
           a.last_interaction_at > now()-interval '1 hour' OR a.background_enabled OR a.explicit_work_until>now()
       ), false) AS eligible,
       CASE WHEN a.last_interaction_at > now()-interval '15 minutes'
            THEN 60 ELSE 300 END AS poll_seconds
FROM aios.instance_goal_activity a
JOIN aios.character_agent_runtime ar USING(instance_id);

-- Range scans for independent arrival/completion counters, without the former
-- created_at OR updated_at predicate across all historical jobs every five seconds.
CREATE INDEX IF NOT EXISTS idx_pipeline_telemetry_arrivals ON aios.pipeline_job(created_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_telemetry_completed ON aios.pipeline_job(updated_at)
    WHERE status='done';
CREATE INDEX IF NOT EXISTS idx_pipeline_telemetry_failed ON aios.pipeline_job(updated_at)
    WHERE status='failed';
