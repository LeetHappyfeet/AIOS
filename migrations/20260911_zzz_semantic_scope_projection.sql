-- Coalesced semantic-topology RDF projection
-- 2026-09-11
--
-- PostgreSQL semantic topology remains authoritative. Per-claim/per-acquisition
-- derivation marks a scope dirty; a single scope-level job rewrites Fuseki.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.semantic_scope_projection_state (
    scope_key text PRIMARY KEY,
    scope_kind text NOT NULL,
    rdf_dataset text,
    rdf_graph text,
    dirty_version bigint NOT NULL DEFAULT 0 CHECK (dirty_version >= 0),
    projected_version bigint NOT NULL DEFAULT 0 CHECK (projected_version >= 0),
    status text NOT NULL DEFAULT 'dirty'
        CHECK (status IN ('dirty','projecting','ready','error')),
    dirty_at timestamptz,
    projected_at timestamptz,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_semantic_scope_projection_dirty
    ON aios.semantic_scope_projection_state (status, dirty_version, projected_version, updated_at)
    WHERE dirty_version > projected_version;

-- enqueue_job() uses INSERT ... ON CONFLICT DO NOTHING.  This partial unique
-- index makes that operation an atomic coalescing boundary for one active RDF
-- projection job per semantic scope.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_project_semantic_scope_active
    ON aios.pipeline_job ((payload->>'scope_key'))
    WHERE job_type='project_semantic_scope'
      AND status IN ('queued','running');

COMMENT ON TABLE aios.semantic_scope_projection_state IS
    'Dirty/version ledger for coalesced semantic topology RDF projection. PostgreSQL topology is authoritative; Fuseki is a scope-level projection.';

COMMIT;
