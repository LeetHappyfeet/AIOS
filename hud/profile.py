from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from aios_app.db import Database


@dataclass(frozen=True)
class HUDProfile:
    profile_id: UUID
    profile_name: str
    description: Optional[str]
    token_budget: int
    recent_event_limit: int
    memory_budget: int
    belief_budget: int
    relationship_budget: int
    scene_budget: int
    inventory_budget: int
    rules_budget: int
    goals_budget: int
    entity_hops: int
    semantic_retrieval_limit: int
    deep_memory_limit: int
    include_emotional_state: bool
    include_physical_state: bool
    include_social_state: bool
    include_inventory: bool
    include_relationships: bool
    include_conflicts: bool
    include_provenance: bool
    include_confidence: bool
    meta: dict[str, Any]

    @classmethod
    def from_row(cls, row) -> "HUDProfile":
        return cls(
            profile_id=row["profile_id"],
            profile_name=row["profile_name"],
            description=row["description"],
            token_budget=row["token_budget"],
            recent_event_limit=row["recent_event_limit"],
            memory_budget=row["memory_budget"],
            belief_budget=row["belief_budget"],
            relationship_budget=row["relationship_budget"],
            scene_budget=row["scene_budget"],
            inventory_budget=row["inventory_budget"],
            rules_budget=row["rules_budget"],
            goals_budget=row["goals_budget"],
            entity_hops=row["entity_hops"],
            semantic_retrieval_limit=row["semantic_retrieval_limit"],
            deep_memory_limit=row["deep_memory_limit"],
            include_emotional_state=row["include_emotional_state"],
            include_physical_state=row["include_physical_state"],
            include_social_state=row["include_social_state"],
            include_inventory=row["include_inventory"],
            include_relationships=row["include_relationships"],
            include_conflicts=row["include_conflicts"],
            include_provenance=row["include_provenance"],
            include_confidence=row["include_confidence"],
            meta=dict(row["meta"] or {}),
        )


async def get_profile(db: Database, *, character_id: str) -> HUDProfile:
    row = await db.fetchrow(
        """
        SELECT hp.*
        FROM aios.character_hud_profile chp
        JOIN aios.hud_profile hp ON hp.profile_id=chp.profile_id
        WHERE chp.character_id=$1
        """,
        character_id,
    )
    if not row:
        row = await db.fetchrow(
            "SELECT * FROM aios.hud_profile WHERE profile_name='default'"
        )
    if not row:
        raise RuntimeError("HUD default profile is missing; run migrations")
    return HUDProfile.from_row(row)


async def list_profiles(db: Database) -> list[HUDProfile]:
    rows = await db.fetch(
        "SELECT * FROM aios.hud_profile ORDER BY profile_name"
    )
    return [HUDProfile.from_row(row) for row in rows]


async def save_profile(
    db: Database,
    *,
    profile_name: str,
    description: Optional[str],
    values: dict[str, Any],
) -> HUDProfile:
    allowed = {
        "token_budget", "recent_event_limit", "memory_budget", "belief_budget",
        "relationship_budget", "scene_budget", "inventory_budget",
        "rules_budget", "goals_budget", "entity_hops",
        "semantic_retrieval_limit", "deep_memory_limit",
        "include_emotional_state", "include_physical_state",
        "include_social_state", "include_inventory", "include_relationships",
        "include_conflicts", "include_provenance", "include_confidence",
    }
    clean = {key: value for key, value in values.items() if key in allowed}
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.hud_profile (
            profile_name, description,
            token_budget, recent_event_limit, memory_budget, belief_budget,
            relationship_budget, scene_budget, inventory_budget,
            rules_budget, goals_budget, entity_hops,
            semantic_retrieval_limit, deep_memory_limit,
            include_emotional_state, include_physical_state,
            include_social_state, include_inventory, include_relationships,
            include_conflicts, include_provenance, include_confidence
        )
        VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22
        )
        ON CONFLICT (profile_name) DO UPDATE
        SET description=EXCLUDED.description,
            token_budget=EXCLUDED.token_budget,
            recent_event_limit=EXCLUDED.recent_event_limit,
            memory_budget=EXCLUDED.memory_budget,
            belief_budget=EXCLUDED.belief_budget,
            relationship_budget=EXCLUDED.relationship_budget,
            scene_budget=EXCLUDED.scene_budget,
            inventory_budget=EXCLUDED.inventory_budget,
            rules_budget=EXCLUDED.rules_budget,
            goals_budget=EXCLUDED.goals_budget,
            entity_hops=EXCLUDED.entity_hops,
            semantic_retrieval_limit=EXCLUDED.semantic_retrieval_limit,
            deep_memory_limit=EXCLUDED.deep_memory_limit,
            include_emotional_state=EXCLUDED.include_emotional_state,
            include_physical_state=EXCLUDED.include_physical_state,
            include_social_state=EXCLUDED.include_social_state,
            include_inventory=EXCLUDED.include_inventory,
            include_relationships=EXCLUDED.include_relationships,
            include_conflicts=EXCLUDED.include_conflicts,
            include_provenance=EXCLUDED.include_provenance,
            include_confidence=EXCLUDED.include_confidence,
            updated_at=now()
        RETURNING *
        """,
        profile_name,
        description or None,
        int(clean.get("token_budget", 1600)),
        int(clean.get("recent_event_limit", 12)),
        int(clean.get("memory_budget", 350)),
        int(clean.get("belief_budget", 320)),
        int(clean.get("relationship_budget", 160)),
        int(clean.get("scene_budget", 260)),
        int(clean.get("inventory_budget", 140)),
        int(clean.get("rules_budget", 140)),
        int(clean.get("goals_budget", 140)),
        int(clean.get("entity_hops", 1)),
        int(clean.get("semantic_retrieval_limit", 25)),
        int(clean.get("deep_memory_limit", 0)),
        bool(clean.get("include_emotional_state", True)),
        bool(clean.get("include_physical_state", True)),
        bool(clean.get("include_social_state", True)),
        bool(clean.get("include_inventory", True)),
        bool(clean.get("include_relationships", True)),
        bool(clean.get("include_conflicts", True)),
        bool(clean.get("include_provenance", True)),
        bool(clean.get("include_confidence", True)),
    )
    return HUDProfile.from_row(row)


async def bind_character_profile(
    db: Database,
    *,
    character_id: str,
    profile_name: str,
) -> HUDProfile:
    row = await db.fetchrow(
        "SELECT profile_id FROM aios.hud_profile WHERE profile_name=$1",
        profile_name,
    )
    if not row:
        raise ValueError(f"Unknown HUD profile '{profile_name}'")
    await db.execute(
        """
        INSERT INTO aios.character_hud_profile (character_id, profile_id)
        VALUES ($1,$2)
        ON CONFLICT (character_id) DO UPDATE
        SET profile_id=EXCLUDED.profile_id,
            updated_at=now()
        """,
        character_id,
        row["profile_id"],
    )
    return await get_profile(db, character_id=character_id)
