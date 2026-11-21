-- Preserve profile identity and pinned criteria. The unique constraint makes
-- a pre-existing workspace/task on the same link an explicit migration conflict.
ALTER TABLE aios.workspace_work_profile DISABLE TRIGGER work_profile_immutable;
UPDATE aios.workspace_work_profile
SET purpose = 'task',
    description = CASE
        WHEN description = 'Perform a bounded experimental task in the authorized workspace. Put the task in objective; report actual outcome, evidence and limitations in JSON. Report checks do not prove task success.'
        THEN 'Inspect, edit, or run checks in the workspace.'
        ELSE description
    END
WHERE capability = 'workspace' AND purpose = 'experimental_task';
ALTER TABLE aios.workspace_work_profile ENABLE TRIGGER work_profile_immutable;

ALTER TABLE aios.workspace_registration ADD COLUMN research_ready boolean NOT NULL DEFAULT false;
ALTER TABLE aios.workspace_binding ADD COLUMN research_ready boolean NOT NULL DEFAULT false;
ALTER TABLE aios.workspace_run ADD COLUMN requires_research boolean NOT NULL DEFAULT false;
