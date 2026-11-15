-- Rebuild derived place evidence from existing cached linguistic projections.
UPDATE aios.entity_memory_projection
SET revision=revision+1, retry_after=now()
WHERE built_revision>0 AND policy_version IS DISTINCT FROM 'entity-memory-v3-place-evidence';
