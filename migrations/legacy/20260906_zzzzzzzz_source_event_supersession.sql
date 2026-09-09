-- AIOS source-event supersession for editable/swipe-based clients
-- 2026-09-06
--
-- Preserve immutable ingest/DAG provenance while allowing one logical source
-- message slot (for example a SillyTavern swipe) to replace a prior active
-- alternative without treating both alternatives as sequential lived history.

BEGIN;

ALTER TABLE aios.ingest_event
    ADD COLUMN IF NOT EXISTS superseded_at timestamptz,
    ADD COLUMN IF NOT EXISTS superseded_by_event_id bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname='ingest_event_superseded_by_event_id_fkey'
          AND conrelid='aios.ingest_event'::regclass
    ) THEN
        ALTER TABLE aios.ingest_event
            ADD CONSTRAINT ingest_event_superseded_by_event_id_fkey
            FOREIGN KEY (superseded_by_event_id)
            REFERENCES aios.ingest_event(event_id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ingest_event_active_source_slot
    ON aios.ingest_event (session_id, source, source_event_id, event_id DESC)
    WHERE superseded_at IS NULL
      AND source_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ingest_event_superseded
    ON aios.ingest_event (superseded_at, event_id)
    WHERE superseded_at IS NOT NULL;

COMMIT;
