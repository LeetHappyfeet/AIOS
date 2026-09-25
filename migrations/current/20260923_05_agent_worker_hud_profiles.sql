-- Seed reusable HUD profiles for character-owned cognitive workers.
-- These are intentionally placeholder presentation profiles. Retrieval depth
-- remains cognition-owned and can be specialized later by task policy.

BEGIN;

INSERT INTO aios.hud_profile (
    profile_name, description,
    token_budget, recent_event_limit,
    memory_budget, belief_budget, relationship_budget, scene_budget,
    inventory_budget, rules_budget, goals_budget,
    entity_hops, semantic_retrieval_limit, deep_memory_limit,
    include_emotional_state, include_physical_state, include_social_state,
    include_inventory, include_relationships, include_conflicts,
    include_provenance, include_confidence, meta
)
VALUES
(
    'agent.executive',
    'Placeholder executive worker HUD: broad character state for deciding the next bounded action.',
    2200, 12,
    420, 380, 220, 300,
    100, 180, 260,
    1, 25, 0,
    true, true, true,
    false, true, true,
    true, true,
    '{"purpose":"executive","worker_profile":true,"placeholder":true}'::jsonb
),
(
    'agent.research',
    'Placeholder research worker HUD: provenance-rich knowledge and evidence with reduced physical context.',
    2600, 8,
    420, 420, 120, 160,
    0, 180, 180,
    1, 25, 0,
    false, false, false,
    false, true, true,
    true, true,
    '{"purpose":"research","worker_profile":true,"placeholder":true}'::jsonb
),
(
    'agent.planning',
    'Placeholder planning worker HUD: goals, beliefs, memories, rules, relationships, and current scene.',
    2400, 10,
    480, 440, 200, 260,
    80, 240, 360,
    1, 25, 0,
    true, true, true,
    false, true, true,
    true, true,
    '{"purpose":"planning","worker_profile":true,"placeholder":true}'::jsonb
),
(
    'agent.reflection',
    'Placeholder reflection worker HUD: memory, belief, goals, and identity continuity with minimal world detail.',
    2200, 8,
    600, 520, 180, 120,
    0, 160, 260,
    0, 25, 0,
    true, false, true,
    false, true, true,
    true, true,
    '{"purpose":"reflection","worker_profile":true,"placeholder":true}'::jsonb
),
(
    'agent.communication',
    'Placeholder communication worker HUD for email, messaging, and other relationship-aware external dialogue.',
    2200, 16,
    420, 360, 320, 180,
    0, 140, 220,
    1, 25, 0,
    true, false, true,
    false, true, true,
    true, true,
    '{"purpose":"communication","worker_profile":true,"placeholder":true}'::jsonb
)
ON CONFLICT (profile_name) DO NOTHING;

COMMIT;
