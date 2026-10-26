-- Tentative executive plans are not epistemic or world authority.
CREATE TABLE aios.character_goal_plan (
 plan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 instance_id uuid NOT NULL,
 goal_id uuid NOT NULL,
 attempt_id uuid NOT NULL,
 revision integer NOT NULL CHECK (revision > 0),
 input_fingerprint text NOT NULL,
 source_timeline_id uuid,
 source_node_id uuid REFERENCES aios.dag_node(node_id),
 source_state_version bigint NOT NULL,
 operation_id uuid REFERENCES aios.character_cognitive_operation(operation_id),
 status text NOT NULL DEFAULT 'queued' CHECK (status IN
   ('queued','ready','waiting_information','failed','stale','superseded','closed')),
 proposal jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(proposal)='object'),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(attempt_id,goal_id,instance_id)
   REFERENCES aios.character_goal_attempt(attempt_id,goal_id,instance_id),
 UNIQUE(instance_id,goal_id,attempt_id,input_fingerprint),
 UNIQUE(instance_id,goal_id,attempt_id,revision)
);
CREATE INDEX idx_goal_plan_current ON aios.character_goal_plan(instance_id,goal_id,revision DESC);
CREATE UNIQUE INDEX uq_goal_plan_queued ON aios.character_goal_plan(instance_id) WHERE status='queued';

CREATE FUNCTION aios.invalidate_goal_plan_hud() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 UPDATE aios.character_hud_readiness SET status='dirty',
   dirty_since=COALESCE(dirty_since,now()),updated_at=now()
 WHERE instance_id=NEW.instance_id;
 RETURN NEW;
END $$;
CREATE TRIGGER invalidate_goal_plan_hud AFTER INSERT OR UPDATE ON aios.character_goal_plan
 FOR EACH ROW EXECUTE FUNCTION aios.invalidate_goal_plan_hud();

CREATE FUNCTION aios.close_inactive_goal_plans() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.status <> 'active' THEN
   UPDATE aios.character_goal_plan SET status='closed',updated_at=now()
   WHERE goal_id=NEW.goal_id AND instance_id=NEW.instance_id
     AND status IN ('queued','ready','waiting_information');
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER close_inactive_goal_plans AFTER UPDATE OF status ON aios.character_agent_goal
 FOR EACH ROW EXECUTE FUNCTION aios.close_inactive_goal_plans();

-- Rotate cheap admission checks fairly across instances; no inference on unchanged inputs.
CREATE TABLE aios.character_goal_plan_poll (
 instance_id uuid PRIMARY KEY REFERENCES aios.character_instance(instance_id),
 checked_at timestamptz NOT NULL DEFAULT now()
);
