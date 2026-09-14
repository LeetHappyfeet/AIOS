-- Typed semantic facet layer between reconciled atoms and compact memory retrieval.
--
-- Facets organize compatible memories without merging away their underlying atoms.
-- Generic descriptive predicates such as `be` are deliberately multi-valued.
-- Exclusivity is attached to narrow semantic slots, not broad grammatical predicates.

BEGIN;

CREATE OR REPLACE FUNCTION aios.semantic_facet_descriptor(
    p_claim_kind text,
    p_predicate_family text,
    p_predicate_norm text
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN lower(COALESCE(p_predicate_norm,'')) IN ('located_at','location') THEN
            jsonb_build_object('facet','LOCATION','slot','current_location','exclusive',true,'policy','typed-slot-v1')
        WHEN lower(COALESCE(p_predicate_norm,''))='born_in' THEN
            jsonb_build_object('facet','IDENTITY','slot','birthplace','exclusive',true,'policy','typed-slot-v1')
        WHEN lower(COALESCE(p_predicate_norm,''))='identity' THEN
            jsonb_build_object('facet','IDENTITY','slot','identity','exclusive',true,'policy','typed-slot-v1')
        WHEN lower(COALESCE(p_predicate_norm,''))='status' THEN
            jsonb_build_object('facet','STATE','slot','status','exclusive',true,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_claim_kind,''))='GOAL' THEN
            jsonb_build_object('facet','GOAL','slot','goal','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_claim_kind,''))='RULE' THEN
            jsonb_build_object('facet','RULE','slot','rule','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_claim_kind,''))='EVENT' THEN
            jsonb_build_object('facet','EVENT','slot','event','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_claim_kind,''))='MEMORY' THEN
            jsonb_build_object('facet','MEMORY','slot','memory','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_claim_kind,''))='TRAIT' THEN
            jsonb_build_object('facet','TRAIT','slot','descriptive_trait','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_predicate_family,''))='SPATIAL' THEN
            jsonb_build_object('facet','LOCATION','slot','spatial_relation','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_predicate_family,'')) IN ('SOCIAL','MEMBERSHIP') THEN
            jsonb_build_object('facet','RELATIONSHIP','slot','social_relation','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_predicate_family,''))='POSSESSION' THEN
            jsonb_build_object('facet','POSSESSION','slot','possession','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_predicate_family,''))='ACTION' THEN
            jsonb_build_object('facet','ACTION','slot','action','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_predicate_family,''))='IDENTITY' THEN
            jsonb_build_object('facet','IDENTITY','slot','descriptive_identity','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_claim_kind,''))='STATE' THEN
            jsonb_build_object('facet','STATE','slot','descriptive_state','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_claim_kind,''))='BELIEF' THEN
            jsonb_build_object('facet','BELIEF','slot','belief','exclusive',false,'policy','typed-slot-v1')
        WHEN upper(COALESCE(p_claim_kind,''))='CONCEPT' THEN
            jsonb_build_object('facet','CONCEPT','slot','concept','exclusive',false,'policy','typed-slot-v1')
        ELSE
            jsonb_build_object('facet','OTHER','slot','descriptive','exclusive',false,'policy','typed-slot-v1')
    END;
$$;

COMMENT ON FUNCTION aios.semantic_facet_descriptor(text,text,text) IS
'Conservative deterministic facet/slot classifier. Broad descriptive predicates remain multi-valued; only narrow typed slots are marked exclusive.';

CREATE OR REPLACE VIEW aios.semantic_memory_facet_member AS
SELECT
    sms.surface_key,
    sms.scope_kind,
    sms.scope_key,
    sms.character_id,
    sms.instance_id,
    sms.world_id,
    sms.atom_id,
    sms.preferred_proposition_id,
    sms.memory_kind,
    sms.stance,
    sms.confidence,
    sms.positive_support,
    sms.negative_support,
    sms.evidence_count,
    sms.independent_evidence_count,
    sms.canonical_text,
    sms.subject_norm,
    sms.predicate_norm,
    sms.object_norm,
    sms.polarity,
    COALESCE(ctx.predicate_family,'UNKNOWN') AS predicate_family,
    fd.descriptor->>'facet' AS facet,
    fd.descriptor->>'slot' AS facet_slot,
    COALESCE((fd.descriptor->>'exclusive')::boolean,false) AS facet_exclusive,
    sms.updated_at,
    sms.meta || jsonb_build_object(
        'facet',fd.descriptor->>'facet',
        'facet_slot',fd.descriptor->>'slot',
        'facet_exclusive',COALESCE((fd.descriptor->>'exclusive')::boolean,false),
        'facet_policy',fd.descriptor->>'policy'
    ) AS meta
FROM aios.semantic_memory_surface sms
LEFT JOIN LATERAL (
    SELECT ccr.predicate_family
    FROM aios.observation o
    JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
    WHERE o.proposition_id=sms.preferred_proposition_id
    ORDER BY ccr.resolved_at DESC NULLS LAST, o.observed_at DESC
    LIMIT 1
) ctx ON true
CROSS JOIN LATERAL (
    SELECT aios.semantic_facet_descriptor(
        sms.memory_kind,
        COALESCE(ctx.predicate_family,'UNKNOWN'),
        sms.predicate_norm
    ) AS descriptor
) fd;

COMMENT ON VIEW aios.semantic_memory_facet_member IS
'One reconciled semantic atom per row, organized into a typed facet and slot without deleting or merging the underlying memory atom.';

CREATE OR REPLACE VIEW aios.semantic_memory_facet AS
SELECT
    scope_kind,
    scope_key,
    character_id,
    instance_id,
    world_id,
    subject_norm,
    facet,
    facet_slot,
    bool_or(facet_exclusive) AS facet_exclusive,
    COUNT(*) AS member_count,
    COUNT(*) FILTER (WHERE stance='unresolved') AS contested_member_count,
    MAX(confidence) AS max_confidence,
    AVG(confidence) AS mean_confidence,
    MAX(updated_at) AS updated_at,
    array_agg(atom_id ORDER BY confidence DESC NULLS LAST, updated_at DESC, atom_id) AS atom_ids,
    array_agg(preferred_proposition_id ORDER BY confidence DESC NULLS LAST, updated_at DESC, preferred_proposition_id) AS proposition_ids,
    jsonb_agg(
        jsonb_build_object(
            'atom_id',atom_id,
            'proposition_id',preferred_proposition_id,
            'text',canonical_text,
            'predicate',predicate_norm,
            'object',object_norm,
            'stance',stance,
            'confidence',confidence,
            'polarity',polarity,
            'updated_at',updated_at
        )
        ORDER BY confidence DESC NULLS LAST, updated_at DESC, atom_id
    ) AS members
FROM aios.semantic_memory_facet_member
GROUP BY
    scope_kind, scope_key, character_id, instance_id, world_id,
    subject_norm, facet, facet_slot;

COMMENT ON VIEW aios.semantic_memory_facet IS
'Grouped higher-order semantic memory facets. Compatible atoms coexist under one typed facet/slot; exclusivity describes whether values in that slot may compete within the same context.';

COMMIT;
