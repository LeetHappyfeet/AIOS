-- Semantic hygiene shadow pass. Does not modify propositions, evidence,
-- character beliefs, topology, or Fuseki. Immutable source-linked audit
-- permits comparing successive policies without rewriting historical claims.
CREATE TABLE IF NOT EXISTS aios.semantic_hygiene_shadow_audit (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    atom_id uuid NOT NULL,
    population text NOT NULL CHECK (population IN ('v3_only','v4_sample')),
    policy_version text NOT NULL,
    source_signature text NOT NULL,
    disposition text NOT NULL CHECK (disposition IN
        ('retain','repair_candidate','demote_candidate','retraction_candidate','needs_review')),
    reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_claim_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_revision_keys jsonb NOT NULL DEFAULT '[]'::jsonb,
    rdf_graphs jsonb NOT NULL DEFAULT '[]'::jsonb,
    impact jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (atom_id, policy_version, source_signature)
);
CREATE INDEX IF NOT EXISTS semantic_hygiene_audit_population_idx
ON aios.semantic_hygiene_shadow_audit (policy_version, population, created_at DESC);

CREATE TABLE IF NOT EXISTS aios.semantic_hygiene_shadow_cursor (
    population text NOT NULL,
    policy_version text NOT NULL,
    last_atom_id uuid,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (population, policy_version),
    CHECK (population IN ('v3_only','v4_sample'))
);
INSERT INTO aios.semantic_hygiene_shadow_cursor(population, policy_version)
VALUES ('v3_only','semantic-hygiene-shadow-v1'),
       ('v4_sample','semantic-hygiene-shadow-v1')
ON CONFLICT DO NOTHING;

COMMENT ON TABLE aios.semantic_hygiene_shadow_audit IS
    'Read-only RDF-assisted candidate discovery and proposed impact; never an authority for live deletion.';
