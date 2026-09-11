BEGIN;

-- Source perception is instance-owned. A runtime may only point at a liminal
-- timeline whose conversation identity matches its own runtime timeline.
UPDATE aios.character_runtime_state rs
SET source_timeline_id=NULL,
    source_head_node_id=NULL,
    updated_at=now()
FROM aios.timeline rt,
     aios.timeline st,
     aios.world sw
WHERE rt.timeline_id=rs.timeline_id
  AND st.timeline_id=rs.source_timeline_id
  AND sw.world_id=st.world_id
  AND (
        sw.world_key <> 'liminal'
        OR st.session_id IS DISTINCT FROM rt.session_id
        OR st.character_id IS DISTINCT FROM rt.character_id
        OR st.user_name IS DISTINCT FROM rt.user_name
        OR st.scope_key IS DISTINCT FROM rt.scope_key
      );

-- Runtime worlds are shared objective/session topology. Historical activation
-- code incorrectly used these anchor columns as a per-user perception cursor.
-- Clear only anchors written by ordinary runtime activation; explicit future
-- world-fork topology can use dedicated branch-origin semantics instead.
UPDATE aios.world
SET anchor_timeline_id=NULL,
    anchor_node_id=NULL
WHERE world_type='runtime'
  AND COALESCE(meta->>'source','')='runtime_activation';

COMMIT;
