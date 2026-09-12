BEGIN;

CREATE OR REPLACE FUNCTION aios.refresh_message_enrichment_cursor()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_commit record;
BEGIN
    IF NEW.projected_at IS NULL THEN
        RETURN NEW;
    END IF;

    FOR target_commit IN
        SELECT DISTINCT
            mcc.commit_id,
            mcc.instance_id,
            mcc.node_id,
            mcc.event_id,
            mcc.character_id
        FROM aios.message_cognitive_commit mcc
        WHERE
            (NEW.claim_id IS NOT NULL AND EXISTS (
                SELECT 1
                FROM aios.claim_candidate cc
                JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
                JOIN aios.document_section ds ON ds.section_id=es.section_id
                WHERE cc.claim_id=NEW.claim_id
                  AND ds.node_id=mcc.node_id
            ))
            OR
            (NEW.acquisition_id IS NOT NULL AND EXISTS (
                SELECT 1
                FROM aios.knowledge_acquisition_event kae
                JOIN aios.claim_candidate cc ON cc.claim_id=kae.claim_id
                JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
                JOIN aios.document_section ds ON ds.section_id=es.section_id
                WHERE kae.acquisition_id=NEW.acquisition_id
                  AND kae.instance_id=mcc.instance_id
                  AND ds.node_id=mcc.node_id
            ))
    LOOP
        -- Every evidence claim for this message must be normalized and have its
        -- claim topology projected before archaeology can be called complete.
        IF EXISTS (
            SELECT 1
            FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            WHERE ds.node_id=target_commit.node_id
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM aios.observation o
                      WHERE o.claim_id=cc.claim_id
                  )
                  OR NOT EXISTS (
                      SELECT 1
                      FROM aios.semantic_topology_projection stp
                      WHERE stp.claim_id=cc.claim_id
                        AND stp.resolver_version='semantic-topology-v1'
                        AND stp.projected_at IS NOT NULL
                  )
              )
        ) THEN
            CONTINUE;
        END IF;

        -- Any character-owned cognition from this source must also have a
        -- processed acquisition and acquisition topology for this exact runtime.
        IF EXISTS (
            SELECT 1
            FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
            WHERE ds.node_id=target_commit.node_id
              AND ccr.epistemic_scope='character'
              AND ccr.origin_character_id=target_commit.character_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM aios.knowledge_acquisition_event kae
                  WHERE kae.instance_id=target_commit.instance_id
                    AND kae.claim_id=cc.claim_id
                    AND kae.processed_at IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM aios.semantic_topology_projection astp
                        WHERE astp.acquisition_id=kae.acquisition_id
                          AND astp.resolver_version='semantic-topology-v1'
                          AND astp.projected_at IS NOT NULL
                    )
              )
        ) THEN
            CONTINUE;
        END IF;

        UPDATE aios.message_cognitive_commit
        SET enrichment_completed_at=COALESCE(enrichment_completed_at, now())
        WHERE commit_id=target_commit.commit_id;

        UPDATE aios.character_hud_readiness
        SET enrichment_ready_node_id=target_commit.node_id,
            enrichment_ready_event_id=target_commit.event_id,
            updated_at=now()
        WHERE instance_id=target_commit.instance_id
          AND (
              enrichment_ready_event_id IS NULL
              OR target_commit.event_id IS NULL
              OR enrichment_ready_event_id <= target_commit.event_id
          );
    END LOOP;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_message_enrichment_cursor
ON aios.semantic_topology_projection;

CREATE TRIGGER trg_refresh_message_enrichment_cursor
AFTER INSERT OR UPDATE OF projected_at
ON aios.semantic_topology_projection
FOR EACH ROW
WHEN (NEW.projected_at IS NOT NULL)
EXECUTE FUNCTION aios.refresh_message_enrichment_cursor();

COMMIT;
