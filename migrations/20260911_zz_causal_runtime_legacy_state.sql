-- Move remaining legacy physical runtime values into hidden causal state.
-- Emotional/social/goals/tasks remain /char runtime state; physical hard state
-- is no longer available as a second mutable authority beside the kernel.

BEGIN;

INSERT INTO aios.causal_state (
    world_id, timeline_id, domain_id, entity_id, state_key, value_json,
    state_version, last_node_id, occurred_at, meta
)
SELECT
    rs.world_id,
    rs.timeline_id,
    'world.scalar',
    we.entity_id,
    valueset.state_key,
    valueset.value_json,
    1,
    rs.head_node_id,
    rs.updated_at,
    jsonb_build_object('bootstrap','legacy_character_runtime_state')
FROM aios.character_runtime_state rs
JOIN aios.world_entity we
  ON we.character_instance_id=rs.instance_id
CROSS JOIN LATERAL (
    VALUES
        ('health'::text, to_jsonb(rs.health)),
        ('stamina'::text, to_jsonb(rs.stamina)),
        ('energy'::text, to_jsonb(rs.energy)),
        ('physical_state'::text, NULLIF(rs.physical_state, '{}'::jsonb))
) AS valueset(state_key, value_json)
WHERE valueset.value_json IS NOT NULL
ON CONFLICT (world_id,timeline_id,domain_id,entity_id,state_key) DO NOTHING;

UPDATE aios.character_runtime_state
SET health=NULL,
    stamina=NULL,
    energy=NULL,
    physical_state='{}'::jsonb,
    updated_at=now()
WHERE health IS NOT NULL
   OR stamina IS NOT NULL
   OR energy IS NOT NULL
   OR physical_state <> '{}'::jsonb;

COMMIT;
