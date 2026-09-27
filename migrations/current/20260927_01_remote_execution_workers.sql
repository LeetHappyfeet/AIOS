-- Remote execution workers: AIOS owns action intent/lifecycle; clients own execution.
BEGIN;

ALTER TABLE aios.character_action
  ADD COLUMN IF NOT EXISTS execution_mode text NOT NULL DEFAULT 'local'
    CHECK (execution_mode IN ('local','worker')),
  ADD COLUMN IF NOT EXISTS assigned_worker_id uuid NULL,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz NULL;

ALTER TABLE aios.character_action DROP CONSTRAINT IF EXISTS character_action_status_check;
ALTER TABLE aios.character_action
  ADD CONSTRAINT character_action_status_check CHECK (status IN (
    'proposed','validated','waiting','queued','running',
    'succeeded','failed','rejected','cancelled','timed_out'
  ));

CREATE TABLE IF NOT EXISTS aios.remote_worker (
  worker_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  worker_key text NOT NULL UNIQUE,
  worker_type text NOT NULL,
  capabilities text[] NOT NULL DEFAULT ARRAY[]::text[],
  labels jsonb NOT NULL DEFAULT '{}'::jsonb,
  max_concurrency integer NOT NULL DEFAULT 1 CHECK (max_concurrency > 0),
  status text NOT NULL DEFAULT 'ready'
    CHECK (status IN ('ready','draining','offline','disabled')),
  last_heartbeat_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE aios.character_action
  DROP CONSTRAINT IF EXISTS character_action_assigned_worker_id_fkey;
ALTER TABLE aios.character_action
  ADD CONSTRAINT character_action_assigned_worker_id_fkey
  FOREIGN KEY (assigned_worker_id) REFERENCES aios.remote_worker(worker_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_character_action_worker_queue
  ON aios.character_action (execution_mode,status,created_at)
  WHERE execution_mode='worker' AND status='queued';
CREATE INDEX IF NOT EXISTS idx_character_action_worker_lease
  ON aios.character_action (assigned_worker_id,status,lease_expires_at)
  WHERE assigned_worker_id IS NOT NULL;

COMMIT;
