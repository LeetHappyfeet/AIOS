-- Stage 3: character-scoped progressive research, bounded work and durable receipts.
-- Dossier/question/source/selection are research provenance, never belief authority.
BEGIN;
CREATE TABLE IF NOT EXISTS aios.character_research_dossier (
    dossier_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    character_id text NOT NULL REFERENCES aios.character_identity(character_id) ON DELETE CASCADE,
    focus_key text NOT NULL,
    question text NOT NULL CHECK (length(btrim(question)) BETWEEN 3 AND 600),
    topic_id uuid REFERENCES aios.knowledge_topic(topic_id) ON DELETE SET NULL,
    origin text NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual','cognition','goal')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','paused','closed')),
    max_cycles smallint NOT NULL DEFAULT 8 CHECK (max_cycles BETWEEN 1 AND 16),
    max_sections smallint NOT NULL DEFAULT 24 CHECK (max_sections BETWEEN 1 AND 48),
    max_materializations smallint NOT NULL DEFAULT 4 CHECK (max_materializations BETWEEN 0 AND 8),
    cycles_completed integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(instance_id,focus_key)
);
CREATE INDEX IF NOT EXISTS idx_research_dossier_resume
    ON aios.character_research_dossier(instance_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS aios.character_research_question (
    question_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_id uuid NOT NULL REFERENCES aios.character_research_dossier(dossier_id) ON DELETE CASCADE,
    question_key text NOT NULL,
    query_text text NOT NULL CHECK(length(btrim(query_text)) BETWEEN 2 AND 600),
    origin text NOT NULL CHECK(origin IN ('initial','topic_followup','manual')),
    source_topic_id uuid REFERENCES aios.knowledge_topic(topic_id) ON DELETE SET NULL,
    source_section_id uuid REFERENCES aios.corpus_section(section_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','explored')),
    priority smallint NOT NULL DEFAULT 100,
    created_at timestamptz NOT NULL DEFAULT now(),
    explored_at timestamptz,
    UNIQUE(dossier_id,question_key)
);
CREATE INDEX IF NOT EXISTS idx_research_question_queue
    ON aios.character_research_question(dossier_id,status,priority DESC,created_at);

CREATE TABLE IF NOT EXISTS aios.character_research_step (
    step_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_id uuid NOT NULL REFERENCES aios.character_research_dossier(dossier_id) ON DELETE CASCADE,
    question_id uuid NOT NULL REFERENCES aios.character_research_question(question_id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    research_id uuid REFERENCES aios.character_research_event(research_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
    query_text text NOT NULL,
    source_count smallint NOT NULL DEFAULT 0,
    newly_discovered smallint NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz NOT NULL DEFAULT (now()+interval '2 minutes'),
    completed_at timestamptz,
    UNIQUE(dossier_id,request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_one_running_step
    ON aios.character_research_step(dossier_id) WHERE status='running';
CREATE INDEX IF NOT EXISTS idx_research_step_dossier
    ON aios.character_research_step(dossier_id,started_at DESC);

CREATE TABLE IF NOT EXISTS aios.character_research_source (
    dossier_id uuid NOT NULL REFERENCES aios.character_research_dossier(dossier_id) ON DELETE CASCADE,
    section_id uuid NOT NULL REFERENCES aios.corpus_section(section_id) ON DELETE CASCADE,
    document_id uuid NOT NULL REFERENCES aios.corpus_document(document_id) ON DELETE CASCADE,
    first_research_id uuid REFERENCES aios.character_research_event(research_id) ON DELETE SET NULL,
    last_research_id uuid REFERENCES aios.character_research_event(research_id) ON DELETE SET NULL,
    best_score double precision NOT NULL DEFAULT 0,
    retrieval_methods text[] NOT NULL DEFAULT '{}'::text[],
    status text NOT NULL DEFAULT 'discovered' CHECK(status IN ('discovered','submitted','failed')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(dossier_id,section_id)
);
CREATE INDEX IF NOT EXISTS idx_research_source_documents
    ON aios.character_research_source(dossier_id,document_id);

CREATE TABLE IF NOT EXISTS aios.character_research_selection (
    selection_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_id uuid NOT NULL REFERENCES aios.character_research_dossier(dossier_id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','submitted','failed')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(dossier_id,request_id)
);
CREATE TABLE IF NOT EXISTS aios.character_research_materialization (
    dossier_id uuid NOT NULL REFERENCES aios.character_research_dossier(dossier_id) ON DELETE CASCADE,
    section_id uuid NOT NULL,
    selection_id uuid NOT NULL REFERENCES aios.character_research_selection(selection_id) ON DELETE CASCADE,
    consumption_id uuid REFERENCES aios.source_consumption(consumption_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','submitted','failed')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(dossier_id,section_id),
    FOREIGN KEY(dossier_id,section_id)
        REFERENCES aios.character_research_source(dossier_id,section_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_research_materialization_selection
    ON aios.character_research_materialization(selection_id);

COMMENT ON TABLE aios.character_research_dossier IS
'Character-specific bounded research state. Not a shared world fact, belief or ontology.';
COMMENT ON TABLE aios.character_research_source IS
'Authorized source-reference inventory; discovery alone does not mean character study or acquisition.';
COMMENT ON TABLE aios.character_research_materialization IS
'Explicit source-consumption submission receipt. Submitted does not mean Integrity V4 approved.';
COMMIT;
