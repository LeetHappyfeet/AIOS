BEGIN;

CREATE TABLE aios.workspace_memory_note (
    run_id uuid NOT NULL REFERENCES aios.workspace_run(run_id) ON DELETE CASCADE,
    slot smallint NOT NULL CHECK (slot BETWEEN 0 AND 2),
    kind text NOT NULL CHECK (kind IN ('attempt', 'finding', 'idea')),
    note_text text NOT NULL CHECK (char_length(note_text) BETWEEN 1 AND 240),
    fingerprint text NOT NULL,
    finding_index smallint CHECK (finding_index BETWEEN 0 AND 5),
    source_url text CHECK (char_length(source_url) <= 512),
    search_terms tsvector GENERATED ALWAYS AS (to_tsvector('english', note_text)) STORED,
    PRIMARY KEY (run_id, slot),
    UNIQUE (run_id, fingerprint)
);

CREATE INDEX workspace_memory_search ON aios.workspace_memory_note USING gin(search_terms);

COMMIT;
