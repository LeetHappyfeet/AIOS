-- Acknowledgment ledger for RDF-visible authority state, not SQL reconciliation
-- timestamps. The writer advances this receipt only after a successful update.
CREATE TABLE IF NOT EXISTS aios.rdf_observation_authority_projection (
    rdf_dataset text NOT NULL,
    rdf_graph text NOT NULL,
    observation_iri text NOT NULL,
    projection_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rdf_dataset, rdf_graph, observation_iri)
);
