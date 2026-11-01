-- External interaction is distinct from worker activity and foreground renewal.
CREATE TABLE aios.instance_goal_activity (
    instance_id uuid PRIMARY KEY REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    last_interaction_at timestamptz,
    explicit_work_until timestamptz,
    background_enabled boolean NOT NULL DEFAULT false,
    last_planning_admitted_at timestamptz,
    last_continuation_admitted_at timestamptz,
    last_continuation_started_at timestamptz
);
INSERT INTO aios.instance_goal_activity(instance_id,last_interaction_at)
SELECT instance_id,updated_at FROM aios.pipeline_foreground_lineage
ON CONFLICT(instance_id) DO NOTHING;

ALTER TABLE aios.character_goal_plan
    ADD COLUMN continuation_signature text,
    ADD COLUMN continuation_unchanged_checks integer NOT NULL DEFAULT 0;

-- This view is also the operator's read-only explanation of scheduling status.
CREATE VIEW aios.instance_goal_scheduling AS
SELECT a.*, ar.state AS runtime_state,
       CASE WHEN a.last_interaction_at > now()-interval '15 minutes' THEN 0
            WHEN a.last_interaction_at > now()-interval '1 hour' THEN 1
            WHEN a.background_enabled OR a.explicit_work_until>now() THEN 2 ELSE 3 END AS activity_band,
       ar.state <> 'paused' AND (
           a.last_interaction_at > now()-interval '1 hour' OR a.background_enabled OR a.explicit_work_until>now()
       ) AS eligible,
       CASE WHEN a.last_interaction_at > now()-interval '15 minutes'
            THEN 60 ELSE 300 END AS poll_seconds
FROM aios.instance_goal_activity a
JOIN aios.character_agent_runtime ar USING(instance_id);
