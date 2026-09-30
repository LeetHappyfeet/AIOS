-- Evidence remains authoritative about what was observed. Only resolved,
-- standalone frames may support independent semantic topology.
BEGIN;
CREATE OR REPLACE FUNCTION aios.semantic_occurrence_topology_eligible(
    requested_claim uuid, requested_proposition uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM aios.observation o
        JOIN aios.observation_proposition op ON op.observation_id=o.observation_id
        JOIN aios.claim_semantic_frame f ON f.frame_id=op.frame_id
        JOIN aios.semantic_interpretation si ON si.frame_id=f.frame_id
        WHERE o.claim_id=requested_claim AND op.proposition_id=requested_proposition
          AND f.claim_id=o.claim_id AND si.claim_id=o.claim_id
          AND f.resolution_status='resolved' AND si.standalone_semantic
    )
$$;
CREATE OR REPLACE FUNCTION aios.semantic_proposition_topology_eligible(requested_proposition uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM aios.observation_proposition op
        JOIN aios.observation o ON o.observation_id=op.observation_id
        JOIN aios.claim_semantic_frame f ON f.frame_id=op.frame_id
        JOIN aios.semantic_interpretation si ON si.frame_id=f.frame_id
        WHERE op.proposition_id=requested_proposition
          AND f.claim_id=o.claim_id AND si.claim_id=o.claim_id
          AND f.resolution_status='resolved' AND si.standalone_semantic
    )
$$;
CREATE OR REPLACE FUNCTION aios.semantic_claim_topology_eligible(requested_claim uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM aios.observation o
        WHERE o.claim_id=requested_claim
          AND aios.semantic_occurrence_topology_eligible(o.claim_id,o.proposition_id))
$$;
CREATE OR REPLACE FUNCTION aios.semantic_claim_topology_admitted(requested_claim uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT aios.semantic_claim_topology_eligible(requested_claim) AND EXISTS (
        SELECT 1 FROM aios.semantic_exact_admission sea
        WHERE sea.claim_id=requested_claim AND (
            sea.decision='reinforces_exact' OR (sea.decision='novel_exact' AND EXISTS (
                SELECT 1 FROM aios.semantic_neighbor_admission sna
                WHERE sna.claim_id=sea.claim_id
                  AND sna.decision IN ('reinforces','refines','challenges','novel')))))
$$;
CREATE OR REPLACE FUNCTION aios.semantic_proposition_topology_admitted(requested_proposition uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM aios.observation o
        JOIN aios.observation_proposition op ON op.observation_id=o.observation_id
        WHERE op.proposition_id=requested_proposition
          AND aios.semantic_occurrence_topology_eligible(o.claim_id,requested_proposition)
          AND aios.semantic_claim_topology_admitted(o.claim_id))
$$;
-- Read-only audit includes legacy claims with missing frame interpretations.
CREATE OR REPLACE VIEW aios.semantic_topology_eligibility_audit AS
SELECT p.proposition_id, p.canonical_text,
       aios.semantic_proposition_topology_eligible(p.proposition_id) AS topology_eligible
FROM aios.proposition p;
COMMIT;
