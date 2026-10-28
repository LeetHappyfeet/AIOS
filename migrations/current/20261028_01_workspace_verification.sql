-- Protocol v2: immutable operator criteria, worker evidence, and separate review.
ALTER TABLE aios.workspace_run ADD COLUMN assignment jsonb NOT NULL DEFAULT '{}';
ALTER TABLE aios.workspace_run ADD COLUMN assignment_hash text;
ALTER TABLE aios.workspace_run DROP CONSTRAINT workspace_run_status_check;
ALTER TABLE aios.workspace_run ADD CONSTRAINT workspace_run_status_check
 CHECK(status IN ('queued','running','finished','succeeded','failed','cancelled','interrupted'));
CREATE TABLE aios.workspace_review (
 run_id uuid PRIMARY KEY REFERENCES aios.workspace_run(run_id),
 goal_id uuid NOT NULL REFERENCES aios.character_agent_goal(goal_id),
 attempt_id uuid NOT NULL REFERENCES aios.character_goal_attempt(attempt_id),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
 decision jsonb,
 reviewed_at timestamptz
);
CREATE FUNCTION aios.workspace_assignment_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.assignment IS DISTINCT FROM OLD.assignment OR NEW.assignment_hash IS DISTINCT FROM OLD.assignment_hash
    OR NEW.objective IS DISTINCT FROM OLD.objective THEN
  RAISE EXCEPTION 'workspace assignment is immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER workspace_assignment_immutable BEFORE UPDATE ON aios.workspace_run
 FOR EACH ROW EXECUTE FUNCTION aios.workspace_assignment_immutable();
