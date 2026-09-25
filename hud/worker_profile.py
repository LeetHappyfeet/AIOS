from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerHUDProfile:
    """Presentation policy for a bounded internal-cognition prompt.

    Retrieval remains owned by CognitiveContextService/HUDAssembler.  These
    profiles only decide which already-resolved cognition is exposed to a
    worker and how much prompt space it may consume.
    """

    name: str
    token_budget: int = 420
    memory_items: int = 2
    belief_items: int = 2
    goal_items: int = 1
    recent_event_items: int = 1
    include_scene: bool = True
    include_memories: bool = True
    include_beliefs: bool = True
    include_goals: bool = True


WORKER_HUD_PROFILES: dict[str, WorkerHUDProfile] = {
    "attention": WorkerHUDProfile(
        name="attention", token_budget=360, memory_items=1, belief_items=1,
        goal_items=1, recent_event_items=1,
    ),
    "reflection": WorkerHUDProfile(
        name="reflection", token_budget=440, memory_items=2, belief_items=2,
        goal_items=1, recent_event_items=1,
    ),
    "recall": WorkerHUDProfile(
        name="recall", token_budget=420, memory_items=3, belief_items=1,
        goal_items=1, recent_event_items=1,
    ),
    "inquiry": WorkerHUDProfile(
        name="inquiry", token_budget=380, memory_items=1, belief_items=2,
        goal_items=0, recent_event_items=1,
    ),
    "planning": WorkerHUDProfile(
        name="planning", token_budget=460, memory_items=2, belief_items=1,
        goal_items=2, recent_event_items=1,
    ),
    "executive": WorkerHUDProfile(
        name="executive", token_budget=420, memory_items=1, belief_items=1,
        goal_items=2, recent_event_items=1,
    ),
}


def get_worker_hud_profile(name: str | None) -> WorkerHUDProfile:
    key = str(name or "attention").strip().lower()
    return WORKER_HUD_PROFILES.get(key, WORKER_HUD_PROFILES["attention"])
