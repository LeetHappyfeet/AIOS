-- Force one semantic-frame-v2 refresh under the perspective-aware resolver
-- without changing the frame storage/version contract consumed downstream.
BEGIN;

UPDATE aios.claim_semantic_frame_projection
SET decomposer_version='semantic-frame-v2-perspective-stale',
    projected_at=now(),
    meta=COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
        'refresh_reason','perspective_resolver_upgrade',
        'target_decomposer_version','semantic-frame-v2',
        'target_resolver_version','local-dag-referent-v3'
    )
WHERE decomposer_version='semantic-frame-v2'
  AND COALESCE(resolver_version,'') <> 'local-dag-referent-v3';

COMMIT;
