-- Foreground causal scheduling for interactive character work.
-- Foregroundness is a short lease, not a new scheduling lane: resource/lane
-- semantics remain unchanged while recent interactive descendants are selected first.

CREATE TABLE IF NOT EXISTS aios.pipeline_foreground_lineage (
    instance_id uuid PRIMARY KEY REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    character_id text NOT NULL,
    timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
    first_event_id bigint NOT NULL,
    head_event_id bigint NOT NULL,
    head_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    activated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    reason text NOT NULL DEFAULT 'interactive_ingest',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_foreground_lineage_timeline
    ON aios.pipeline_foreground_lineage(timeline_id, first_event_id, head_event_id, expires_at);

ALTER TABLE aios.pipeline_job
    ADD COLUMN IF NOT EXISTS foreground_instance_id uuid REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS foreground_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS foreground_until timestamptz;

CREATE INDEX IF NOT EXISTS idx_pipeline_job_foreground_runnable
    ON aios.pipeline_job(resource_class, scheduling_lane, foreground_until, priority, created_at)
    WHERE status='queued';

-- Existing queued jobs are intentionally left ordinary. New enqueue decisions
-- derive foreground ancestry from the source coordinate; stale foregroundness
-- therefore cannot be manufactured by this migration.
