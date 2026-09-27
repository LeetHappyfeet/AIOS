from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

from aios_app.db import Database


class WorldProjectionCoordinator:
    """Project committed /world changes into character-relative derived state.

    The causal kernel remains ignorant of /char. Callers invoke this only after
    a successful causal/world commit. Scene projection is therefore a
    consequence of reality changing, never a HUD side effect.
    """

    def __init__(self, db: Database):
        self.db = db

    async def world_state_changed(
        self,
        *,
        world_id: UUID,
        timeline_id: UUID,
        domain_id: str,
        entity_id: UUID,
        before: Any,
        after: Any,
    ) -> list[UUID]:
        if domain_id != "world.location":
            return []

        location_ids: list[UUID] = []
        for value in (before, after):
            try:
                if value is not None:
                    location_ids.append(UUID(str(value)))
            except (TypeError, ValueError):
                continue

        rows = await self.db.fetch(
            """SELECT DISTINCT ci.instance_id
               FROM aios.character_instance ci
               JOIN aios.character_runtime_state rs ON rs.instance_id=ci.instance_id
               JOIN aios.world_entity we ON we.character_instance_id=ci.instance_id
               LEFT JOIN aios.world_entity_relation loc
                 ON loc.world_id=$1
                AND loc.subject_entity_id=we.entity_id
                AND loc.relation_type='located_in'
                AND loc.valid_to_node_id IS NULL
               WHERE rs.world_id=$1
                 AND rs.timeline_id=$2
                 AND (
                      we.entity_id=$3
                      OR loc.object_entity_id=ANY($4::uuid[])
                 )""",
            world_id,timeline_id,entity_id,location_ids,
        )
        instance_ids=[row["instance_id"] for row in rows]

        # Import here so causal/world code does not acquire a module-level /char
        # dependency and create a circular ownership boundary.
        from aios_app.epistemic.scene_resolver import CharacterSceneProjector
        projector=CharacterSceneProjector(self.db)
        for instance_id in instance_ids:
            await projector.refresh(instance_id)
        return instance_ids
