BEGIN;

ALTER TABLE aios.character_temporal_trigger
  DROP CONSTRAINT character_temporal_trigger_status_check,
  ADD CHECK (status IN ('scheduled','firing','fired','cancelled','completed','timeline_inactive')),
  ADD COLUMN invalid_decisions integer NOT NULL DEFAULT 0 CHECK (invalid_decisions >= 0);

CREATE INDEX idx_temporal_suspended ON aios.character_temporal_trigger(instance_id,due_at)
  WHERE status='timeline_inactive';

CREATE TABLE aios.character_reminder_continuation (
  trigger_id uuid PRIMARY KEY REFERENCES aios.character_temporal_trigger(trigger_id) ON DELETE CASCADE,
  instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
  goal_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  source_timeline_id uuid NOT NULL,
  plan_id uuid,
  source_node_id uuid,
  input_fingerprint text,
  intent jsonb,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','submitted','closed')),
  decision_note text,
  work_request_id uuid REFERENCES aios.character_work_request(request_id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_reminder_continuation_instance ON aios.character_reminder_continuation(instance_id,created_at DESC);

ALTER TABLE aios.character_work_request
  ADD COLUMN reminder_trigger_id uuid REFERENCES aios.character_reminder_continuation(trigger_id) ON DELETE SET NULL;

COMMIT;
