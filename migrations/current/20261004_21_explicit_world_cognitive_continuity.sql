-- Explicit episode/world continuity; source timelines never imply shared reality.
-- Immutable forward-only correction to the legacy character-wide default.
BEGIN;

-- The old trigger linked every consecutive session with the same character root,
-- regardless of incompatible stories. Keep existing explicitly authored edges.
DROP TRIGGER IF EXISTS trg_link_runtime_world_continuity ON aios.world;
DROP FUNCTION IF EXISTS aios.link_runtime_world_continuity();
DELETE FROM aios.world_relation
WHERE relation='continues' AND COALESCE(meta->>'automatic','false')='true';

-- Scope cognitive evidence to the current instance lineage plus compatible
-- worlds connected by an explicit HISTORY continuation. Character-wide policy
-- no longer means every alternate-world incarnation of that character.
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
eligible AS (
    SELECT l.instance_id,l.depth FROM lineage l
    UNION ALL
    SELECT r.instance_id,
           100000+row_number() OVER (ORDER BY r.created_at DESC,r.instance_id)::integer
    FROM related r
)
SELECT e.instance_id,MIN(e.depth)::integer AS depth
FROM eligible e GROUP BY e.instance_id
ORDER BY MIN(e.depth),e.instance_id;
$$;

COMMENT ON FUNCTION aios.cognitive_evidence_instances(uuid) IS
'Instance lineage plus explicitly compatible world-history continuity; never all same-character alternate worlds.';

-- Recompute materialized belief winners: old evidence must not survive merely
-- because the new scope no longer retrieves it. Preserve original acquisitions.
DO $$
DECLARE rec record;
BEGIN
    FOR rec IN
        SELECT target.instance_id, atoms.atom_id
        FROM aios.character_instance target
        CROSS JOIN LATERAL (
            SELECT DISTINCT p.atom_id
            FROM aios.character_proposition_knowledge cpk
            JOIN aios.character_instance origin ON origin.instance_id=cpk.instance_id
            JOIN aios.proposition p ON p.proposition_id=cpk.proposition_id
            WHERE origin.character_id=target.character_id AND p.atom_id IS NOT NULL
            UNION
            SELECT bs.atom_id FROM aios.character_belief_state bs
            WHERE bs.instance_id=target.instance_id
        ) atoms
        ORDER BY target.instance_id,atoms.atom_id
    LOOP
        PERFORM aios.reconcile_character_belief_atom(rec.instance_id,rec.atom_id);
    END LOOP;
END;
$$;
COMMIT;
