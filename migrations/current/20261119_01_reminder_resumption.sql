BEGIN;

ALTER TABLE aios.character_reminder_continuation
  DROP CONSTRAINT character_reminder_continuation_status_check;

UPDATE aios.character_reminder_continuation SET status='waiting_work' WHERE status='submitted';
UPDATE aios.character_reminder_continuation SET status='pending'
 WHERE status='closed' AND decision_note IN (
   'Character chose to wait.', 'No workspace profile or source anchor is available.',
   'Existing work is pending on this attempt.', 'Invalid work proposal.');

ALTER TABLE aios.character_reminder_continuation
  ADD CHECK (status IN ('pending','waiting_plan','waiting_information','waiting_configuration',
                       'waiting_work','waiting_evidence','awaiting_review','failed','closed')),
  ADD COLUMN next_evaluation_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN wait_signature text,
  ADD COLUMN unchanged_checks integer NOT NULL DEFAULT 0 CHECK (unchanged_checks>=0),
  ADD COLUMN invalid_decisions integer NOT NULL DEFAULT 0 CHECK (invalid_decisions>=0),
  ADD COLUMN generation integer NOT NULL DEFAULT 0 CHECK (generation>=0),
  ADD COLUMN receipt_revision text,
  ADD COLUMN reviewed_receipt_revision text,
  ADD COLUMN review_attempt integer NOT NULL DEFAULT 0 CHECK (review_attempt>=0);

CREATE INDEX idx_reminder_continuation_due
ON aios.character_reminder_continuation(next_evaluation_at,instance_id)
WHERE status NOT IN ('failed','closed');

COMMIT;
