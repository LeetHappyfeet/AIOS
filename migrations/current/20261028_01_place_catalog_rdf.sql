BEGIN;
-- Re-emit the derived RDF navigation vocabulary from existing payloads.
-- No parsing, identity merges, world entities, grants, or acquisitions.
UPDATE aios.entity_memory_projection SET rdf_revision=0 WHERE built_revision>0;
UPDATE aios.corpus_reference_projection SET rdf_revision=0 WHERE built_revision>0;
COMMIT;
