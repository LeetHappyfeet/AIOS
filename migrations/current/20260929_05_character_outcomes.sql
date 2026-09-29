-- Atomic goal attempts/outcomes; old terminal history is deliberately not relabelled.
BEGIN;
ALTER TABLE aios.character_agent_goal DROP CONSTRAINT IF EXISTS character_agent_goal_status_check;
ALTER TABLE aios.character_agent_goal ADD CONSTRAINT character_agent_goal_status_check
 CHECK (status IN ('active','scheduled','dormant','completed','failed','cancelled'));

CREATE TABLE aios.character_goal_attempt (
 attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES aios.character_agent_goal(goal_id),
 instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
 attempt_number integer NOT NULL CHECK (attempt_number>0),
 retry_request_id uuid,
 source_timeline_id uuid,
 contract_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
 expectation_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
 goal_text_snapshot text NOT NULL,
 started_at timestamptz NOT NULL DEFAULT now(),
 closed_at timestamptz,
 UNIQUE(goal_id,attempt_number), UNIQUE(goal_id,retry_request_id),
 UNIQUE(attempt_id,goal_id,instance_id)
);
CREATE UNIQUE INDEX uq_character_goal_open_attempt ON aios.character_goal_attempt(goal_id) WHERE closed_at IS NULL;

CREATE FUNCTION aios.guard_character_attempt_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'attempt history is immutable'; END IF;
 IF (NEW.attempt_id,NEW.goal_id,NEW.instance_id,NEW.attempt_number,NEW.retry_request_id,
     NEW.source_timeline_id,NEW.contract_snapshot,NEW.expectation_snapshot,NEW.goal_text_snapshot,NEW.started_at)
    IS DISTINCT FROM (OLD.attempt_id,OLD.goal_id,OLD.instance_id,OLD.attempt_number,OLD.retry_request_id,
     OLD.source_timeline_id,OLD.contract_snapshot,OLD.expectation_snapshot,OLD.goal_text_snapshot,OLD.started_at)
    OR (OLD.closed_at IS NOT NULL AND NEW.closed_at IS DISTINCT FROM OLD.closed_at) THEN
   RAISE EXCEPTION 'attempt snapshots are immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_character_attempt_snapshot BEFORE UPDATE OR DELETE ON aios.character_goal_attempt
 FOR EACH ROW EXECUTE FUNCTION aios.guard_character_attempt_snapshot();

CREATE TABLE aios.character_attempt_strategy (
 attempt_id uuid PRIMARY KEY REFERENCES aios.character_goal_attempt(attempt_id),
 strategy_key text NOT NULL CHECK (length(strategy_key) BETWEEN 1 AND 120),
 receipt_type text NOT NULL CHECK (receipt_type IN ('action','cognitive_operation')),
 receipt_id uuid NOT NULL,
 linked_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE aios.character_outcome_event (
 outcome_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 instance_id uuid NOT NULL,
 subject_type text NOT NULL DEFAULT 'goal' CHECK (subject_type='goal'),
 subject_id uuid NOT NULL,
 attempt_id uuid NOT NULL,
 outcome_type text NOT NULL CHECK (outcome_type IN ('success','failure','abandoned','unresolved')),
 verification text NOT NULL CHECK (verification IN ('unverified','reviewed','projected','verified')),
 cause_class text NOT NULL DEFAULT 'unknown',
 attribution text NOT NULL DEFAULT 'unknown' CHECK (attribution IN ('unknown','self','external','mixed')),
 source_node_id uuid,
 source_timeline_id uuid,
 evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_ids)='array'),
 resolution_kind text NOT NULL,
 policy_version text NOT NULL DEFAULT 'outcomes-v1',
 supersedes_outcome_id uuid UNIQUE REFERENCES aios.character_outcome_event(outcome_id),
 dedupe_key text NOT NULL,
 meta jsonb NOT NULL DEFAULT '{}'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(attempt_id,subject_id,instance_id) REFERENCES aios.character_goal_attempt(attempt_id,goal_id,instance_id),
 UNIQUE(instance_id,dedupe_key)
);
CREATE FUNCTION aios.guard_outcome_correction_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE previous aios.character_outcome_event;
BEGIN
 IF NEW.supersedes_outcome_id IS NOT NULL THEN
   SELECT * INTO previous FROM aios.character_outcome_event WHERE outcome_id=NEW.supersedes_outcome_id;
   IF NOT FOUND OR (NEW.instance_id,NEW.subject_id,NEW.attempt_id,NEW.source_timeline_id)
      IS DISTINCT FROM (previous.instance_id,previous.subject_id,previous.attempt_id,previous.source_timeline_id) THEN
     RAISE EXCEPTION 'outcome correction must preserve attempt and timeline scope';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_outcome_correction_scope BEFORE INSERT ON aios.character_outcome_event
 FOR EACH ROW EXECUTE FUNCTION aios.guard_outcome_correction_scope();
CREATE UNIQUE INDEX uq_character_attempt_terminal_outcome ON aios.character_outcome_event(attempt_id)
 WHERE supersedes_outcome_id IS NULL;
CREATE INDEX idx_character_outcome_instance ON aios.character_outcome_event(instance_id,created_at DESC);
CREATE TABLE aios.character_appraisal_pending (
 outcome_id uuid PRIMARY KEY REFERENCES aios.character_outcome_event(outcome_id),
 created_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION aios.guard_character_goal_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.goal_id<>OLD.goal_id OR NEW.instance_id<>OLD.instance_id THEN
   RAISE EXCEPTION 'goal ownership is immutable';
 END IF;
 IF OLD.status IN ('completed','failed','cancelled') AND NEW.status<>OLD.status THEN
   IF NEW.status<>'active' OR NEW.meta->>'retry_request_id' IS NULL
      OR NEW.meta->>'retry_request_id' IS NOT DISTINCT FROM OLD.meta->>'retry_request_id' THEN
     RAISE EXCEPTION 'terminal goal requires explicit new retry request';
   END IF;
 END IF;
 IF OLD.status NOT IN ('completed','failed','cancelled') AND
    (NEW.meta->'completion_contract' IS DISTINCT FROM OLD.meta->'completion_contract'
     OR NEW.meta->'failure_contract' IS DISTINCT FROM OLD.meta->'failure_contract'
     OR NEW.meta->'goal_contract' IS DISTINCT FROM OLD.meta->'goal_contract'
     OR NEW.meta->'expectation' IS DISTINCT FROM OLD.meta->'expectation') THEN
   RAISE EXCEPTION 'attempt contracts and expectations are frozen; create a new goal';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_character_goal_attempt BEFORE UPDATE ON aios.character_agent_goal
 FOR EACH ROW EXECUTE FUNCTION aios.guard_character_goal_attempt();

CREATE FUNCTION aios.project_character_goal_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a aios.character_goal_attempt; decision jsonb; oid uuid; timeline uuid; retry_id uuid;
BEGIN
 IF TG_OP='INSERT' OR (OLD.status IN ('completed','failed','cancelled') AND NEW.status='active') THEN
   IF NEW.status IN ('active','scheduled','dormant') THEN
     SELECT timeline_id INTO timeline FROM aios.dag_node WHERE node_id=NEW.source_node_id;
     IF TG_OP='UPDATE' THEN retry_id:=(NEW.meta->>'retry_request_id')::uuid; END IF;
     INSERT INTO aios.character_goal_attempt(goal_id,instance_id,attempt_number,retry_request_id,
       source_timeline_id,contract_snapshot,expectation_snapshot,goal_text_snapshot)
     VALUES(NEW.goal_id,NEW.instance_id,
       COALESCE((SELECT max(attempt_number)+1 FROM aios.character_goal_attempt WHERE goal_id=NEW.goal_id),1),
       retry_id,timeline,jsonb_build_object('success',COALESCE(NEW.meta->'goal_contract'->'success',NEW.meta->'completion_contract'),
         'failure',COALESCE(NEW.meta->'goal_contract'->'failure',NEW.meta->'failure_contract')),
       COALESCE(NEW.meta->'expectation','{}'::jsonb),NEW.goal_text);
   END IF;
   IF TG_OP='UPDATE' THEN
     UPDATE aios.character_runtime_state SET state_version=state_version+1,updated_at=now() WHERE instance_id=NEW.instance_id;
     UPDATE aios.character_hud_readiness SET status='dirty',dirty_since=COALESCE(dirty_since,now()),updated_at=now()
       WHERE instance_id=NEW.instance_id;
   END IF;
   RETURN NEW;
 END IF;
 IF NEW.status=OLD.status THEN RETURN NEW; END IF;
 IF NEW.status IN ('completed','failed','cancelled','dormant') THEN
   -- Goal row is already locked by UPDATE. All these effects share its transaction.
   UPDATE aios.character_temporal_trigger SET status='cancelled',cancelled_at=COALESCE(cancelled_at,now()),
     last_evaluated_at=now(),last_outcome='goal_'||NEW.status,updated_at=now()
     WHERE goal_id=NEW.goal_id AND status IN ('scheduled','firing');
   UPDATE aios.character_cognitive_thread SET status='resolved',resolved_at=COALESCE(resolved_at,now()),
     pressure=0,meta=meta||jsonb_build_object('resolution_kind',COALESCE(NEW.meta->>'resolution_kind',NEW.status),
       'resolved_by','goal_lifecycle'),updated_at=now()
     WHERE instance_id=NEW.instance_id AND goal_id=NEW.goal_id AND status<>'resolved';
 END IF;
 IF NEW.status IN ('completed','failed','cancelled') THEN
   SELECT * INTO a FROM aios.character_goal_attempt WHERE goal_id=NEW.goal_id AND closed_at IS NULL FOR UPDATE;
   IF NOT FOUND THEN RAISE EXCEPTION 'goal has no open attempt'; END IF;
   decision:=COALESCE(NEW.meta->'outcome_decision','{}'::jsonb);
   -- Lifecycle callers cannot mark learning evidence verified by setting metadata.
   INSERT INTO aios.character_outcome_event(instance_id,subject_id,attempt_id,outcome_type,verification,
     source_node_id,source_timeline_id,evidence_ids,resolution_kind,dedupe_key,meta)
   VALUES(NEW.instance_id,NEW.goal_id,a.attempt_id,
     CASE NEW.status WHEN 'completed' THEN 'success' WHEN 'failed' THEN 'failure' ELSE 'abandoned' END,
     CASE decision->>'verification' WHEN 'reviewed' THEN 'reviewed' WHEN 'projected' THEN 'projected' ELSE 'unverified' END,
     NULLIF(decision->>'source_node_id','')::uuid,a.source_timeline_id,
     COALESCE(decision->'evidence_ids','[]'::jsonb),COALESCE(NEW.meta->>'resolution_kind',NEW.status),
     'goal-attempt:'||a.attempt_id::text,jsonb_build_object('goal_status',NEW.status)) RETURNING outcome_id INTO oid;
   UPDATE aios.character_goal_attempt SET closed_at=now() WHERE attempt_id=a.attempt_id;
   INSERT INTO aios.character_appraisal_pending(outcome_id) VALUES(oid);
 END IF;
 UPDATE aios.character_runtime_state SET state_version=state_version+1,updated_at=now() WHERE instance_id=NEW.instance_id;
 UPDATE aios.character_hud_readiness SET status='dirty',dirty_since=COALESCE(dirty_since,now()),updated_at=now()
   WHERE instance_id=NEW.instance_id;
 RETURN NEW;
END $$;
-- Only live intentions get backfilled attempts; no invented historical outcomes.
INSERT INTO aios.character_goal_attempt(goal_id,instance_id,attempt_number,source_timeline_id,
 contract_snapshot,expectation_snapshot,goal_text_snapshot)
 SELECT g.goal_id,g.instance_id,1,n.timeline_id,
 jsonb_build_object('success',COALESCE(g.meta->'goal_contract'->'success',g.meta->'completion_contract'),
 'failure',COALESCE(g.meta->'goal_contract'->'failure',g.meta->'failure_contract')),
 '{}'::jsonb,g.goal_text FROM aios.character_agent_goal g LEFT JOIN aios.dag_node n ON n.node_id=g.source_node_id
 WHERE g.status IN ('active','scheduled','dormant');
CREATE TRIGGER project_character_goal_outcome AFTER INSERT OR UPDATE OF status ON aios.character_agent_goal
 FOR EACH ROW EXECUTE FUNCTION aios.project_character_goal_outcome();

CREATE FUNCTION aios.reject_outcome_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'outcome history is append-only; append a correction'; END $$;
CREATE TRIGGER immutable_character_outcome BEFORE UPDATE OR DELETE ON aios.character_outcome_event
 FOR EACH ROW EXECUTE FUNCTION aios.reject_outcome_ledger_mutation();
CREATE TRIGGER immutable_character_attempt_strategy BEFORE UPDATE OR DELETE ON aios.character_attempt_strategy
 FOR EACH ROW EXECUTE FUNCTION aios.reject_outcome_ledger_mutation();
-- Operation receipts retain the attempt they were prepared to serve. A late
-- bounded review must never close a newer retry of the same intention.
ALTER TABLE aios.character_cognitive_operation ADD COLUMN goal_attempt_id uuid
 REFERENCES aios.character_goal_attempt(attempt_id);
CREATE FUNCTION aios.bind_cognitive_operation_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.goal_attempt_id:=NULL;
 SELECT a.attempt_id INTO NEW.goal_attempt_id FROM aios.character_goal_attempt a
 JOIN aios.character_agent_goal g USING(goal_id)
 WHERE g.goal_id::text=NEW.input->>'goal_id' AND g.instance_id=NEW.instance_id AND a.closed_at IS NULL;
 RETURN NEW;
END $$;
CREATE TRIGGER bind_cognitive_operation_attempt BEFORE INSERT ON aios.character_cognitive_operation
 FOR EACH ROW EXECUTE FUNCTION aios.bind_cognitive_operation_attempt();
-- Freeze typed receipt content when first acquired. Later metadata changes
-- cannot retroactively manufacture a different outcome from the same evidence.
CREATE TABLE aios.character_outcome_receipt (
 acquisition_id uuid PRIMARY KEY REFERENCES aios.knowledge_acquisition_event(acquisition_id),
 instance_id uuid NOT NULL,
 source_node_id uuid,
 receipt jsonb NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
 reason text,
 created_at timestamptz NOT NULL DEFAULT now(),
 resolved_at timestamptz
);
CREATE INDEX idx_character_outcome_receipt_pending ON aios.character_outcome_receipt(created_at) WHERE status='pending';
CREATE FUNCTION aios.capture_character_outcome_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF jsonb_typeof(NEW.meta->'outcome_receipt')='object' THEN
   INSERT INTO aios.character_outcome_receipt(acquisition_id,instance_id,source_node_id,receipt)
    VALUES(NEW.acquisition_id,NEW.instance_id,NEW.dag_node_id,NEW.meta->'outcome_receipt') ON CONFLICT DO NOTHING;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_outcome_receipt AFTER INSERT ON aios.knowledge_acquisition_event
 FOR EACH ROW EXECUTE FUNCTION aios.capture_character_outcome_receipt();
CREATE FUNCTION aios.guard_character_outcome_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'outcome receipt is immutable'; END IF;
 IF (NEW.acquisition_id,NEW.instance_id,NEW.source_node_id,NEW.receipt,NEW.created_at)
    IS DISTINCT FROM (OLD.acquisition_id,OLD.instance_id,OLD.source_node_id,OLD.receipt,OLD.created_at) THEN
   RAISE EXCEPTION 'outcome receipt provenance is immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_character_outcome_receipt BEFORE UPDATE OR DELETE ON aios.character_outcome_receipt
 FOR EACH ROW EXECUTE FUNCTION aios.guard_character_outcome_receipt();
COMMENT ON TABLE aios.character_outcome_event IS 'Append-only outcome decisions. Reviewed/projected conclusions are not verified behavioral learning evidence.';
COMMIT;
