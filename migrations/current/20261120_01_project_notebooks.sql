BEGIN;

-- Private project notebook: a notebook survives individual goal attempts.
-- Writes are immutable and source-node/work-request idempotent.
CREATE TABLE IF NOT EXISTS aios.character_notebook (
  notebook_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  instance_id uuid NOT NULL,
  source_timeline_id uuid NOT NULL,
  goal_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(instance_id, source_timeline_id, goal_id)
);

CREATE TABLE IF NOT EXISTS aios.character_notebook_entry (
  entry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  notebook_id uuid NOT NULL REFERENCES aios.character_notebook(notebook_id) ON DELETE CASCADE,
  instance_id uuid NOT NULL,
  source_timeline_id uuid NOT NULL,
  goal_id uuid NOT NULL,
  author_kind text NOT NULL CHECK(author_kind IN ('character','worker')),
  entry_type text NOT NULL CHECK(entry_type IN
      ('note','observation','question','decision','next_step','worker_report')),
  content text NOT NULL CHECK(length(btrim(content)) BETWEEN 1 AND 4000),
  source_node_id uuid,
  work_request_id uuid,
  receipt_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  search_vector tsvector GENERATED ALWAYS AS
      (to_tsvector('english',coalesce(content,''))) STORED,
  UNIQUE(notebook_id,receipt_key),
  CHECK ((author_kind='character' AND source_node_id IS NOT NULL AND work_request_id IS NULL)
      OR (author_kind='worker' AND work_request_id IS NOT NULL AND source_node_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_character_notebook_entry_scope
  ON aios.character_notebook_entry(instance_id,source_timeline_id,goal_id,created_at DESC,entry_id DESC);
CREATE INDEX IF NOT EXISTS ix_character_notebook_entry_search
  ON aios.character_notebook_entry USING GIN(search_vector);

COMMIT;
