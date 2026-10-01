BEGIN;
ALTER TABLE aios.semantic_neighbor_candidate
 ADD COLUMN IF NOT EXISTS classification_ready_at timestamptz NOT NULL DEFAULT '-infinity',
 ADD COLUMN IF NOT EXISTS last_classifier_version text;
ALTER TABLE aios.semantic_structure_state
 ADD COLUMN IF NOT EXISTS retry_after timestamptz NOT NULL DEFAULT '-infinity';
UPDATE aios.semantic_neighbor_candidate c SET last_classifier_version=r.classifier_version
FROM (
 SELECT DISTINCT ON(proposition_id,neighbor_proposition_id,embedding_version)
   proposition_id,neighbor_proposition_id,embedding_version,classifier_version
 FROM aios.semantic_neighbor_relation
 ORDER BY proposition_id,neighbor_proposition_id,embedding_version,created_at DESC
) r WHERE c.proposition_id=r.proposition_id AND c.neighbor_proposition_id=r.neighbor_proposition_id
 AND c.embedding_version=r.embedding_version AND c.last_classifier_version IS NULL;
CREATE INDEX IF NOT EXISTS idx_semantic_candidate_newest_pending
 ON aios.semantic_neighbor_candidate(embedding_version,updated_at DESC,proposition_id,neighbor_proposition_id)
 WHERE status='candidate';
CREATE INDEX IF NOT EXISTS idx_semantic_proposition_newest
 ON aios.proposition(created_at DESC,proposition_id DESC);
CREATE INDEX IF NOT EXISTS idx_semantic_frame_newest_resolved
 ON aios.claim_semantic_frame(created_at DESC,frame_id DESC)
 WHERE decomposer_version='semantic-frame-v2' AND resolution_status='resolved';
CREATE INDEX IF NOT EXISTS idx_semantic_structure_retry ON aios.semantic_structure_state(retry_after);
COMMIT;
