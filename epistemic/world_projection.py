from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from aios_app.hud.context import HUDContext


@dataclass(frozen=True)
class WorldQueryProjection:
    """Character-aware query projected into the public world semantic space."""

    query_text: str
    identity_terms: tuple[str, ...]
    conversation_text: str


async def build_character_world_query(
    db: Any,
    context: HUDContext,
    *,
    focus_text: str = "",
    halo_text: str = "",
    goals: Iterable[Any] = (),
) -> WorldQueryProjection:
    """Build a conservative world query from durable identity + live context.

    Identity supplies stable anchors; conversation and goals determine which part
    of the public world is relevant now.  This function deliberately does not
    grant ownership or character knowledge to anything it retrieves.
    """
    row = await db.fetchrow(
        """
        SELECT canonical_name, display_name, franchise, species, primary_role
        FROM aios.character_identity
        WHERE character_id=$1
        """,
        context.character_id,
    )

    seen: set[str] = set()
    identity: list[str] = []
    if row:
        for key in ("canonical_name", "display_name", "franchise", "species", "primary_role"):
            value = str(row[key] or "").strip()
            normalized = value.casefold()
            if value and normalized not in seen:
                seen.add(normalized)
                identity.append(value)

    conversation = " ".join(
        part.strip()
        for part in (halo_text, focus_text, " ".join(str(goal) for goal in goals))
        if part and part.strip()
    )
    query_text = " ".join((*identity, conversation)).strip()
    return WorldQueryProjection(query_text, tuple(identity), conversation)
