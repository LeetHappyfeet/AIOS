-- Current evidence for branch-local narrative history. This view does not grant
-- authority to change causal state or promote character knowledge.
CREATE VIEW aios.narrated_scene_observation AS
SELECT o.observation_id, o.claim_id, o.proposition_id, o.timeline_id, o.dag_node_id,
       c.world_id, p.canonical_text, p.subject_norm, p.predicate_norm, p.object_norm,
       f.subject_entity_key, f.object_entity_key, f.resolved_subject, f.resolved_object,
       move.movement
FROM aios.observation o
JOIN aios.claim_context_resolution c ON c.claim_id=o.claim_id
JOIN aios.claim_candidate cc ON cc.claim_id=o.claim_id
JOIN aios.proposition p ON p.proposition_id=o.proposition_id
JOIN aios.dag_node n ON n.node_id=o.dag_node_id AND n.timeline_id=o.timeline_id
JOIN aios.timeline t ON t.timeline_id=n.timeline_id AND t.world_id=c.world_id
JOIN aios.ingest_event ie ON ie.event_id=n.event_id
CROSS JOIN LATERAL (
    SELECT p.predicate_norm IN ('enter','arrive','arrive_at','walk_into','walk_to','move_to',
                               'depart','depart_from','leave')
        OR (p.predicate_norm IN ('walk','move','run','step')
            AND cc.raw_text ~* '\m(walk(ed|s)?|mov(e|ed|es)|ran|run(s)?|step(ped|s)?)\s+(into|to|out of|from)\M')
        AS movement
) move
JOIN LATERAL (
    SELECT sf.subject_entity_key, sf.object_entity_key, sf.resolved_subject, sf.resolved_object
    FROM aios.observation_proposition op
    JOIN aios.claim_semantic_frame sf ON sf.frame_id=op.frame_id AND sf.claim_id=o.claim_id
    JOIN aios.semantic_interpretation si ON si.frame_id=sf.frame_id AND si.claim_id=o.claim_id
    WHERE op.observation_id=o.observation_id AND op.proposition_id=o.proposition_id
      AND sf.resolution_status='resolved' AND si.standalone_semantic
      AND sf.polarity=1 AND sf.modality='asserted' AND sf.tense IN ('Past','Pres')
      AND sf.discourse_mode='narrated_observation'
      AND COALESCE(sf.meta->>'inside_direct_quote','false') <> 'true'
      AND NOT aios.semantic_hygiene_occurrence_suppressed(o.claim_id,sf.frame_id,op.proposition_id)
      AND NOT aios.semantic_hygiene_occurrence_pending_review(o.claim_id,sf.frame_id,op.proposition_id)
      AND (si.semantic_type='ACTION' OR (si.semantic_type='LOCATION' AND move.movement))
    ORDER BY sf.frame_index, sf.frame_id
    LIMIT 1
) f ON true
WHERE n.kind='chat_message' AND ie.superseded_at IS NULL
  AND c.epistemic_scope='narrative' AND c.dag_node_id=n.node_id AND c.timeline_id=n.timeline_id
  AND p.polarity=1 AND p.modality='asserted'
  AND cc.raw_text !~* '\m(will|would|shall)\M|[''’]ll\M'
  AND aios.semantic_occurrence_topology_eligible(o.claim_id,o.proposition_id)
  AND aios.semantic_claim_topology_admitted(o.claim_id);

-- Source chats live in the liminal timeline. A runtime can project a source
-- turn only through its own perception and the ancestry of its adopted head.
CREATE FUNCTION aios.narrated_scene_source_visible(
    source_node uuid, source_timeline uuid, source_head uuid
) RETURNS boolean LANGUAGE sql STABLE AS $$
    WITH RECURSIVE ancestry(node_id) AS (
        SELECT source_head WHERE source_head IS NOT NULL
        UNION
        SELECT e.parent_node_id
        FROM ancestry a
        JOIN aios.dag_edge e ON e.child_node_id=a.node_id AND e.timeline_id=source_timeline
        WHERE a.node_id <> source_node
    )
    SELECT EXISTS (SELECT 1 FROM ancestry WHERE node_id=source_node)
$$;

CREATE VIEW aios.narrated_scene_occurrence AS
SELECT o.*, e.semantic_event_id, e.confidence, rs.world_id AS scene_world_id,
       rs.timeline_id AS scene_timeline_id,
       'world:' || rs.world_id::text || ':timeline:' || rs.timeline_id::text || ':narrated-events' AS scope_key
FROM aios.narrated_scene_observation o
JOIN aios.semantic_event_membership m
  ON m.observation_id=o.observation_id AND m.proposition_id=o.proposition_id
 AND m.claim_id=o.claim_id AND m.status='active'
JOIN aios.semantic_event e ON e.semantic_event_id=m.semantic_event_id AND e.status='active'
 AND e.world_id=o.world_id AND e.timeline_id=o.timeline_id AND e.dag_node_id=o.dag_node_id
JOIN aios.message_participant mp ON mp.node_id=o.dag_node_id AND mp.perceived
JOIN aios.conversation_participant cp ON cp.participant_id=mp.participant_id
 AND cp.timeline_id=o.timeline_id
JOIN aios.character_runtime_state rs ON rs.instance_id=cp.character_instance_id
 AND rs.source_timeline_id=o.timeline_id
JOIN aios.timeline rt ON rt.timeline_id=rs.timeline_id AND rt.world_id=rs.world_id
WHERE aios.narrated_scene_source_visible(o.dag_node_id,o.timeline_id,rs.source_head_node_id);

-- The existing RDF mutation trigger records node deletion. Wake the existing
-- publisher as well, including when the last event leaves the graph.
CREATE FUNCTION aios.dirty_deleted_narrated_scene_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM aios.mark_semantic_hygiene_scope_dirty(OLD.scope_key);
    RETURN OLD;
END
$$;

CREATE TRIGGER trg_narrated_scene_event_deleted
AFTER DELETE ON aios.semantic_topology_node
FOR EACH ROW WHEN (OLD.node_type='NARRATED_SCENE_EVENT')
EXECUTE FUNCTION aios.dirty_deleted_narrated_scene_event();
