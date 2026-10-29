-- Purpose-based work admission. Capability grants and check commands are operator owned.
ALTER TABLE aios.workspace_binding ADD COLUMN last_seen_at timestamptz;
CREATE TABLE aios.workspace_work_profile (
 profile_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 link_id uuid NOT NULL REFERENCES aios.character_environment_link(link_id),
 capability text NOT NULL CHECK(capability ~ '^[a-z][a-z0-9_]{0,63}$'),
 purpose text NOT NULL CHECK(purpose ~ '^[a-z][a-z0-9_]{0,63}$'),
 description text NOT NULL,
 assignment_template jsonb NOT NULL,
 enabled boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(link_id,capability,purpose)
);
CREATE TABLE aios.character_work_request (
 request_id uuid PRIMARY KEY,
 instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
 source_timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id),
 source_node_id uuid NOT NULL REFERENCES aios.dag_node(node_id),
 intent jsonb NOT NULL,
 intent_hash text NOT NULL,
 goal_id uuid REFERENCES aios.character_agent_goal(goal_id),
 attempt_id uuid REFERENCES aios.character_goal_attempt(attempt_id),
 status text NOT NULL DEFAULT 'requested'
   CHECK(status IN ('requested','waiting_client','needs_configuration','awaiting_approval','denied','queued','invalidated')),
 reason text,
 profile_id uuid REFERENCES aios.workspace_work_profile(profile_id),
 profile_snapshot jsonb,
 run_id uuid UNIQUE REFERENCES aios.workspace_run(run_id),
 approved_by text,
 decision_reason text,
 decision jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX work_request_source_intent ON aios.character_work_request(instance_id,source_node_id,intent_hash);
CREATE INDEX work_request_pending ON aios.character_work_request(instance_id,created_at)
 WHERE run_id IS NULL AND status NOT IN ('denied','invalidated');
CREATE FUNCTION aios.work_profile_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.link_id,NEW.capability,NEW.purpose,NEW.assignment_template)
    IS DISTINCT FROM (OLD.link_id,OLD.capability,OLD.purpose,OLD.assignment_template) THEN
   RAISE EXCEPTION 'work profile criteria are immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER work_profile_immutable BEFORE UPDATE ON aios.workspace_work_profile
 FOR EACH ROW EXECUTE FUNCTION aios.work_profile_immutable();
CREATE FUNCTION aios.work_request_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.request_id,NEW.instance_id,NEW.source_timeline_id,NEW.source_node_id,NEW.intent,NEW.intent_hash,NEW.goal_id,NEW.attempt_id)
    IS DISTINCT FROM (OLD.request_id,OLD.instance_id,OLD.source_timeline_id,OLD.source_node_id,OLD.intent,OLD.intent_hash,OLD.goal_id,OLD.attempt_id)
    OR (OLD.profile_id IS NOT NULL AND (NEW.profile_id,NEW.profile_snapshot) IS DISTINCT FROM (OLD.profile_id,OLD.profile_snapshot))
    OR (OLD.run_id IS NOT NULL AND NEW.run_id IS DISTINCT FROM OLD.run_id) THEN
   RAISE EXCEPTION 'work request coordinates and pinned criteria are immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER work_request_immutable BEFORE UPDATE ON aios.character_work_request
 FOR EACH ROW EXECUTE FUNCTION aios.work_request_immutable();
-- Retain retry identities when different generations propose the same in-flight intent.
CREATE TABLE aios.work_request_alias (
 request_id uuid PRIMARY KEY,
 canonical_request_id uuid NOT NULL REFERENCES aios.character_work_request(request_id),
 instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
 source_node_id uuid NOT NULL REFERENCES aios.dag_node(node_id),
 intent_hash text NOT NULL
);
CREATE TABLE aios.work_source_receipt (
 instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
 source_node_id uuid NOT NULL REFERENCES aios.dag_node(node_id),
 result jsonb NOT NULL,
 PRIMARY KEY(instance_id,source_node_id)
);
