-- First-class knowledge-domain registry and unresolved structured identifier inventory.
-- Corpus metadata may discover candidates; only registered identifiers resolve to domains.
BEGIN;

CREATE TABLE IF NOT EXISTS aios.knowledge_domain (
    domain_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_key text NOT NULL UNIQUE,
    display_name text NOT NULL,
    domain_kind text NOT NULL DEFAULT 'general',
    parent_domain_id uuid REFERENCES aios.knowledge_domain(domain_id) ON DELETE SET NULL,
    default_scope_key text,
    default_epistemic_namespace text NOT NULL DEFAULT 'reference',
    enabled boolean NOT NULL DEFAULT true,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (domain_key <> ''),
    CHECK (display_name <> ''),
    CHECK (domain_kind <> '')
);

CREATE INDEX IF NOT EXISTS idx_knowledge_domain_parent
    ON aios.knowledge_domain(parent_domain_id)
    WHERE parent_domain_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS aios.knowledge_domain_identifier (
    identifier_type text NOT NULL,
    identifier_value text NOT NULL,
    domain_id uuid NOT NULL REFERENCES aios.knowledge_domain(domain_id) ON DELETE CASCADE,
    source text NOT NULL DEFAULT 'operator',
    confidence double precision NOT NULL DEFAULT 1.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (identifier_type, identifier_value, domain_id),
    CHECK (identifier_type <> ''),
    CHECK (identifier_value <> '')
);

CREATE INDEX IF NOT EXISTS idx_knowledge_domain_identifier_lookup
    ON aios.knowledge_domain_identifier(identifier_type, identifier_value);

CREATE TABLE IF NOT EXISTS aios.knowledge_domain_candidate (
    candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    identifier_type text NOT NULL,
    identifier_value text NOT NULL,
    status text NOT NULL DEFAULT 'unresolved'
        CHECK (status IN ('unresolved','resolved','ignored')),
    occurrence_count bigint NOT NULL DEFAULT 0 CHECK (occurrence_count >= 0),
    first_document_id uuid REFERENCES aios.corpus_document(document_id) ON DELETE SET NULL,
    last_document_id uuid REFERENCES aios.corpus_document(document_id) ON DELETE SET NULL,
    resolved_domain_id uuid REFERENCES aios.knowledge_domain(domain_id) ON DELETE SET NULL,
    source text NOT NULL DEFAULT 'corpus_structured_metadata',
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE (identifier_type, identifier_value),
    CHECK (identifier_type <> ''),
    CHECK (identifier_value <> '')
);

CREATE INDEX IF NOT EXISTS idx_knowledge_domain_candidate_status
    ON aios.knowledge_domain_candidate(status, occurrence_count DESC, last_seen_at DESC);

-- Preserve compatibility with the existing text-key plumbing while making
-- every already-authored route/domain a registry entity.
INSERT INTO aios.knowledge_domain (
    domain_key, display_name, domain_kind, default_scope_key,
    default_epistemic_namespace, meta
)
SELECT DISTINCT
    cfr.knowledge_domain,
    cfr.knowledge_domain,
    CASE WHEN cfr.knowledge_domain LIKE 'fiction.%' THEN 'fictional_universe' ELSE 'general' END,
    cfr.scope_key,
    cfr.epistemic_namespace,
    jsonb_build_object('backfilled_from','corpus_facet_route')
FROM aios.corpus_facet_route cfr
WHERE cfr.knowledge_domain <> ''
ON CONFLICT (domain_key) DO NOTHING;

INSERT INTO aios.knowledge_domain (
    domain_key, display_name, domain_kind, meta
)
SELECT DISTINCT
    ckd.knowledge_domain,
    ckd.knowledge_domain,
    CASE WHEN ckd.knowledge_domain LIKE 'fiction.%' THEN 'fictional_universe' ELSE 'general' END,
    jsonb_build_object('backfilled_from','character_knowledge_domain')
FROM aios.character_knowledge_domain ckd
WHERE ckd.knowledge_domain <> ''
ON CONFLICT (domain_key) DO NOTHING;

INSERT INTO aios.knowledge_domain_identifier (
    identifier_type, identifier_value, domain_id, source, confidence, meta
)
SELECT
    lower(cfr.facet_type),
    lower(regexp_replace(trim(cfr.facet_value), '\\s+', ' ', 'g')),
    kd.domain_id,
    'legacy_facet_route',
    1.0,
    jsonb_build_object(
        'scope_key', cfr.scope_key,
        'epistemic_namespace', cfr.epistemic_namespace,
        'priority', cfr.priority
    )
FROM aios.corpus_facet_route cfr
JOIN aios.knowledge_domain kd ON kd.domain_key=cfr.knowledge_domain
ON CONFLICT (identifier_type, identifier_value, domain_id) DO NOTHING;

COMMENT ON TABLE aios.knowledge_domain IS
'Canonical AIOS information-domain registry. Domain keys remain stable compatibility identifiers while aliases and source-native identifiers resolve through the registry.';
COMMENT ON TABLE aios.knowledge_domain_identifier IS
'Trusted deterministic identifiers/aliases for a knowledge domain. Structured source metadata may resolve through these rows; prose never creates them.';
COMMENT ON TABLE aios.knowledge_domain_candidate IS
'Inventory of previously unknown structured domain identifiers observed during corpus ingestion. Candidates accumulate evidence but grant no access and create no character identity.';

COMMIT;
