BEGIN;

-- Keep the archaeology/enrichment cursor independent from HUD traffic. The
-- message-level cognitive commit is the generation barrier; this function only
-- records when exhaustive claim/acquisition topology has caught up to that same
-- source node.
CREATE OR REPLACE FUNCTION aios.refresh_message_enrichment_readiness(
    p_node_id uuid
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    commit_row record;
    total_claims bigint;
    claim_topology_ready bigint;
    acquisition_required bigint;
    acquisition_topology_ready bigint;
BEGIN
    IF p_node_id IS NULL THEN
        RETURN;
    END IF;

    -- Extraction completion is the boundary at which a zero-claim message can
    -- be considered fully enriched. For non-empty messages the topology counts
    -- below remain the actual completion barrier.
    IF NOT EXISTS (
        SELECT 1
        FROM aios.document_section ds
        WHERE ds.node_id = p_node_id
          AND ds.claims_extracted_at IS NOT NULL
    ) THEN
        RETURN;
    END IF;

    FOR commit_row IN
        SELECT mcc.instance_id, mcc.node_id, mcc.event_id, ci.character_id
        FROM aios.message_cognitive_commit mcc
        JOIN aios.character_instance ci
          ON ci.instance_id = mcc.instance_id
        WHERE mcc.node_id = p_node_id
          AND mcc.enrichment_completed_at IS NULL
    LOOP
        SELECT
            count(DISTINCT cc.claim_id),
            count(DISTINCT CASE
                WHEN stp.projected_at IS NOT NULL THEN cc.claim_id
            END),
            count(DISTINCT CASE
                WHEN ccr.epistemic_scope = 'character'
                 AND ccr.origin_character_id = commit_row.character_id
                THEN cc.claim_id
            END),
            count(DISTINCT CASE
                WHEN ccr.epistemic_scope = 'character'
                 AND ccr.origin_character_id = commit_row.character_id
                 AND ccr.character_instance_id = commit_row.instance_id
                 AND kae.processed_at IS NOT NULL
                 AND astp.projected_at IS NOT NULL
                THEN cc.claim_id
            END)
        INTO
            total_claims,
            claim_topology_ready,
            acquisition_required,
            acquisition_topology_ready
        FROM aios.document_section ds
        LEFT JOIN aios.extracted_sentence es
          ON es.section_id = ds.section_id
        LEFT JOIN aios.claim_candidate cc
          ON cc.sentence_id = es.sentence_id
        LEFT JOIN aios.claim_context_resolution ccr
          ON ccr.claim_id = cc.claim_id
        LEFT JOIN aios.knowledge_acquisition_event kae
          ON kae.claim_id = cc.claim_id
         AND kae.instance_id = commit_row.instance_id
        LEFT JOIN aios.semantic_topology_projection stp
          ON stp.claim_id = cc.claim_id
         AND stp.resolver_version = 'semantic-topology-v1'
        LEFT JOIN aios.semantic_topology_projection astp
          ON astp.acquisition_id = kae.acquisition_id
         AND astp.resolver_version = 'semantic-topology-v1'
        WHERE ds.node_id = p_node_id;

        IF COALESCE(claim_topology_ready, 0) = COALESCE(total_claims, 0)
           AND COALESCE(acquisition_topology_ready, 0) = COALESCE(acquisition_required, 0)
        THEN
            UPDATE aios.message_cognitive_commit
            SET enrichment_completed_at = COALESCE(enrichment_completed_at, now())
            WHERE instance_id = commit_row.instance_id
              AND node_id = commit_row.node_id;

            UPDATE aios.character_hud_readiness
            SET enrichment_ready_node_id = commit_row.node_id,
                enrichment_ready_event_id = commit_row.event_id,
                updated_at = now()
            WHERE instance_id = commit_row.instance_id;
        END IF;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION aios.refresh_message_enrichment_from_projection()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_node_id uuid;
BEGIN
    IF NEW.projected_at IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.claim_id IS NOT NULL THEN
        SELECT ds.node_id
        INTO source_node_id
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es
          ON es.sentence_id = cc.sentence_id
        JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        WHERE cc.claim_id = NEW.claim_id
        LIMIT 1;
    ELSIF NEW.acquisition_id IS NOT NULL THEN
        SELECT ds.node_id
        INTO source_node_id
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.claim_candidate cc
          ON cc.claim_id = kae.claim_id
        JOIN aios.extracted_sentence es
          ON es.sentence_id = cc.sentence_id
        JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        WHERE kae.acquisition_id = NEW.acquisition_id
        LIMIT 1;
    END IF;

    IF source_node_id IS NOT NULL THEN
        PERFORM aios.refresh_message_enrichment_readiness(source_node_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_message_enrichment_projection
    ON aios.semantic_topology_projection;
CREATE TRIGGER trg_refresh_message_enrichment_projection
AFTER INSERT OR UPDATE OF projected_at
ON aios.semantic_topology_projection
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_message_enrichment_from_projection();

CREATE OR REPLACE FUNCTION aios.refresh_message_enrichment_from_section()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.claims_extracted_at IS NOT NULL
       AND OLD.claims_extracted_at IS DISTINCT FROM NEW.claims_extracted_at
    THEN
        PERFORM aios.refresh_message_enrichment_readiness(NEW.node_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_refresh_message_enrichment_section
    ON aios.document_section;
CREATE TRIGGER trg_refresh_message_enrichment_section
AFTER UPDATE OF claims_extracted_at
ON aios.document_section
FOR EACH ROW
EXECUTE FUNCTION aios.refresh_message_enrichment_from_section();

-- Converge already-complete rows during upgrade rather than waiting for a new
-- topology write to happen.
DO $$
DECLARE
    node_row record;
BEGIN
    FOR node_row IN
        SELECT DISTINCT node_id
        FROM aios.message_cognitive_commit
        WHERE enrichment_completed_at IS NULL
    LOOP
        PERFORM aios.refresh_message_enrichment_readiness(node_row.node_id);
    END LOOP;
END;
$$;

COMMIT;
