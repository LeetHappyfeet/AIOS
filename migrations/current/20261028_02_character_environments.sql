-- Explicit concurrent environments; no implicit shared worlds or source adoption.
BEGIN;
CREATE TABLE aios.character_environment_link (
    link_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
    execution_instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
    execution_binding_id uuid NOT NULL REFERENCES aios.workspace_binding(binding_id),
    origin_session_id uuid NOT NULL REFERENCES aios.session(session_id),
    origin_source_timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id),
    share_memory boolean NOT NULL DEFAULT false,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    CHECK(origin_instance_id<>execution_instance_id),
    UNIQUE(origin_instance_id,execution_binding_id)
);
CREATE INDEX environment_link_execution ON aios.character_environment_link(execution_instance_id) WHERE enabled;
ALTER TABLE aios.workspace_run
    ADD COLUMN environment_link_id uuid REFERENCES aios.character_environment_link(link_id),
    ADD COLUMN origin_instance_id uuid REFERENCES aios.character_instance(instance_id),
    ADD COLUMN origin_source_timeline_id uuid REFERENCES aios.timeline(timeline_id),
    ADD COLUMN origin_source_node_id uuid,
    ADD COLUMN required_protocol_version integer NOT NULL DEFAULT 2 CHECK(required_protocol_version IN (2,3)),
    ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false;
CREATE INDEX workspace_run_origin ON aios.workspace_run(origin_instance_id,origin_source_timeline_id,updated_at DESC)
    WHERE environment_link_id IS NOT NULL;
CREATE TABLE aios.workspace_run_event (
    sequence bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES aios.workspace_run(run_id),
    event_key text NOT NULL,
    kind text NOT NULL,
    summary text NOT NULL CHECK(length(summary)<=1000),
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,event_key)
);
CREATE INDEX workspace_event_run ON aios.workspace_run_event(run_id,sequence);
CREATE TABLE aios.character_knowledge_revision (
    instance_id uuid PRIMARY KEY REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    revision bigint NOT NULL DEFAULT 1
);
CREATE FUNCTION aios.bump_character_knowledge_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner_id uuid;
DECLARE old_owner uuid;
BEGIN
    IF TG_TABLE_NAME IN ('epistemic_authority_admission','semantic_evidence_admission') THEN
        SELECT instance_id INTO owner_id FROM aios.knowledge_acquisition_event
        WHERE acquisition_id=CASE WHEN TG_OP='DELETE' THEN OLD.acquisition_id ELSE NEW.acquisition_id END;
    ELSE
        owner_id:=CASE WHEN TG_OP='DELETE' THEN OLD.instance_id ELSE NEW.instance_id END;
        IF TG_OP='UPDATE' THEN old_owner:=OLD.instance_id; END IF;
    END IF;
    INSERT INTO aios.character_knowledge_revision(instance_id)
    SELECT ci.instance_id FROM aios.character_instance ci
    WHERE ci.instance_id=owner_id OR ci.instance_id=old_owner
    ORDER BY ci.instance_id
    ON CONFLICT(instance_id) DO UPDATE SET revision=aios.character_knowledge_revision.revision+1;
    RETURN NULL;
END $$;
CREATE TRIGGER environment_acquisition_revision AFTER INSERT OR UPDATE OR DELETE ON aios.knowledge_acquisition_event
FOR EACH ROW EXECUTE FUNCTION aios.bump_character_knowledge_revision();
CREATE TRIGGER environment_projection_revision AFTER INSERT OR UPDATE OR DELETE ON aios.character_proposition_knowledge
FOR EACH ROW EXECUTE FUNCTION aios.bump_character_knowledge_revision();
CREATE TRIGGER environment_authority_revision AFTER INSERT OR UPDATE OR DELETE ON aios.epistemic_authority_admission
FOR EACH ROW EXECUTE FUNCTION aios.bump_character_knowledge_revision();
CREATE TRIGGER environment_admission_revision AFTER INSERT OR UPDATE OR DELETE ON aios.semantic_evidence_admission
FOR EACH ROW EXECUTE FUNCTION aios.bump_character_knowledge_revision();
CREATE OR REPLACE FUNCTION aios.cognitive_evidence_instances(p_instance_id uuid)
RETURNS TABLE(instance_id uuid, depth integer)
LANGUAGE sql STABLE AS $$
WITH RECURSIVE
target AS (
    SELECT ci.instance_id, ci.character_id,
           COALESCE(ci.current_world_id,ci.world_id) AS world_id,
           ci.meta->>'runtime_user_name' AS runtime_user_name,
           COALESCE(cep.memory_continuity,'character') AS memory_continuity
    FROM aios.character_instance ci
    LEFT JOIN aios.character_epistemic_profile cep
      ON cep.character_id=ci.character_id
    WHERE ci.instance_id=p_instance_id
),
lineage AS (
    SELECT ci.instance_id,ci.parent_instance_id,0 AS depth
    FROM aios.character_instance ci WHERE ci.instance_id=p_instance_id
    UNION ALL
    SELECT parent.instance_id,parent.parent_instance_id,l.depth+1
    FROM lineage l JOIN aios.character_instance parent
      ON parent.instance_id=l.parent_instance_id WHERE l.depth<64
),
compatible_worlds AS (
    SELECT t.world_id,0 AS depth,ARRAY[t.world_id]::uuid[] AS path
    FROM target t
    UNION ALL
    SELECT r.source_world_id,c.depth+1,c.path||r.source_world_id
    FROM compatible_worlds c
    JOIN aios.world_relation r
      ON r.world_id=c.world_id AND r.enabled AND r.relation='continues'
     AND 'history'=ANY(r.domains)
     AND COALESCE(r.meta->>'automatic','false')<>'true'
    WHERE c.depth<8 AND NOT r.source_world_id=ANY(c.path)
),
related AS (
    SELECT DISTINCT ci.instance_id,ci.created_at
    FROM target t
    JOIN aios.character_instance ci ON ci.character_id=t.character_id
    JOIN compatible_worlds w
      ON w.world_id=COALESCE(ci.current_world_id,ci.world_id)
    WHERE t.memory_continuity IN ('character','user')
      AND (t.memory_continuity<>'user'
           OR (t.runtime_user_name IS NOT NULL AND
               ci.meta->>'runtime_user_name'=t.runtime_user_name))
      -- Two personas inside an old shared runtime world never acquire one
      -- another's private episodes just because a transport session was reused.
      AND (w.world_id<>t.world_id OR
           ci.meta->>'runtime_user_name' IS NOT DISTINCT FROM t.runtime_user_name)
),
linked AS (
    -- Direct, explicitly authorized peers only. Never traverse a chain of links.
    SELECT peer.instance_id
    FROM target t
    JOIN aios.character_environment_link el ON el.enabled AND el.share_memory
      AND t.instance_id IN (el.origin_instance_id,el.execution_instance_id)
    JOIN aios.character_instance peer ON peer.instance_id=CASE
      WHEN el.origin_instance_id=t.instance_id THEN el.execution_instance_id
      ELSE el.origin_instance_id END
    LEFT JOIN aios.character_epistemic_profile pp ON pp.character_id=peer.character_id
    WHERE t.memory_continuity IN ('character','user')
      AND COALESCE(pp.memory_continuity,'character')<>'isolated'
      AND peer.character_id=t.character_id
      AND peer.meta->>'runtime_user_name' IS NOT DISTINCT FROM t.runtime_user_name
),
eligible AS (
    SELECT l.instance_id,l.depth FROM lineage l
    UNION ALL
    SELECT r.instance_id,
           100000+row_number() OVER (ORDER BY r.created_at DESC,r.instance_id)::integer
    FROM related r
    UNION ALL
    SELECT instance_id, 200000 FROM linked
)
SELECT e.instance_id,MIN(e.depth)::integer AS depth
FROM eligible e GROUP BY e.instance_id
ORDER BY MIN(e.depth),e.instance_id;
$$;


CREATE FUNCTION aios.cognitive_context_revision(p_instance_id uuid) RETURNS text LANGUAGE sql STABLE AS $$
 SELECT md5(COALESCE(string_agg(e.instance_id::text||':'||COALESCE(r.revision,0)::text,',' ORDER BY e.instance_id),''))
 FROM aios.cognitive_evidence_instances(p_instance_id) e
 LEFT JOIN aios.character_knowledge_revision r USING(instance_id);
$$;
INSERT INTO aios.hud_profile(profile_name,description,token_budget,recent_event_limit,
    memory_budget,belief_budget,relationship_budget,scene_budget,inventory_budget,rules_budget,goals_budget,
    include_emotional_state,include_physical_state,include_social_state,include_inventory,include_relationships)
VALUES('workspace_execution','Task-focused context for a real remote workspace',2500,4,
    800,400,0,0,0,250,400,false,false,false,false,false)
ON CONFLICT(profile_name) DO NOTHING;
COMMIT;
