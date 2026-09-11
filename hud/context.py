from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from aios_app.db import Database


@dataclass(frozen=True)
class HUDContext:
    """Branch-safe /char coordinates used by the HUD resolver."""

    instance_id: UUID
    character_id: str
    entity_id: UUID
    world_id: UUID
    world_key: str
    timeline_id: UUID
    head_node_id: Optional[UUID]
    source_timeline_id: Optional[UUID]
    source_head_node_id: Optional[UUID]
    state_version: int
    lifecycle_state: str
    location_entity_id: Optional[UUID]
    lineage_world_ids: tuple[UUID, ...]
    lineage_instance_ids: tuple[UUID, ...]
    scene_entity_ids: frozenset[UUID]

    def world_visible(self, candidate_world_id: Optional[UUID]) -> bool:
        """Only the current branch and its ancestors are implicitly visible."""
        return candidate_world_id is None or candidate_world_id in self.lineage_world_ids

    def instance_visible(self, candidate_instance_id: Optional[UUID]) -> bool:
        """Only the active experiential branch and its ancestors are implicit memory scope."""
        return candidate_instance_id is None or candidate_instance_id in self.lineage_instance_ids

    def entity_is_active(self, entity_id: Optional[UUID]) -> bool:
        return bool(entity_id and entity_id in self.scene_entity_ids)


class HUDContextResolver:
    """Resolve the subjective frame without reading hidden causal_state.

    SQL /world semantic projection remains the maximum objective input boundary
    for HUD assembly.  The resolver never queries causal_state or causal events
    directly, and sibling worlds remain excluded.
    """

    def __init__(self, db: Database):
        self.db = db

    async def resolve(self, instance_id: UUID) -> HUDContext:
        state = await self.db.fetchrow(
            """
            SELECT
                rs.instance_id,
                rs.world_id,
                rs.timeline_id,
                rs.head_node_id,
                rs.source_timeline_id,
                rs.source_head_node_id,
                rs.state_version,
                rs.lifecycle_state,
                ci.character_id,
                we.entity_id,
                w.world_key,
                loc.object_entity_id AS location_entity_id
            FROM aios.character_runtime_state rs
            JOIN aios.character_instance ci ON ci.instance_id=rs.instance_id
            JOIN aios.world w ON w.world_id=rs.world_id
            LEFT JOIN aios.world_entity we ON we.character_instance_id=rs.instance_id
            LEFT JOIN LATERAL (
                SELECT r.object_entity_id
                FROM aios.world_entity_relation r
                WHERE r.world_id=rs.world_id
                  AND r.subject_entity_id=we.entity_id
                  AND r.relation_type='located_in'
                  AND r.valid_to_node_id IS NULL
                ORDER BY r.created_at DESC
                LIMIT 1
            ) loc ON true
            WHERE rs.instance_id=$1
            """,
            instance_id,
        )
        if not state or not state["entity_id"]:
            raise LookupError(f"Unknown runtime instance {instance_id}")

        lineage = await self.db.fetch(
            """
            WITH RECURSIVE lineage AS (
                SELECT world_id, parent_world_id, 0 AS depth
                FROM aios.world
                WHERE world_id=$1

                UNION ALL

                SELECT w.world_id, w.parent_world_id, l.depth + 1
                FROM aios.world w
                JOIN lineage l ON l.parent_world_id=w.world_id
                WHERE l.depth < 64
            )
            SELECT world_id
            FROM lineage
            ORDER BY depth
            """,
            state["world_id"],
        )
        lineage_world_ids = tuple(row["world_id"] for row in lineage) or (state["world_id"],)

        instance_lineage = await self.db.fetch(
            """
            WITH RECURSIVE lineage AS (
                SELECT instance_id, parent_instance_id, 0 AS depth
                FROM aios.character_instance
                WHERE instance_id=$1

                UNION ALL

                SELECT ci.instance_id, ci.parent_instance_id, l.depth + 1
                FROM aios.character_instance ci
                JOIN lineage l ON l.parent_instance_id=ci.instance_id
                WHERE l.depth < 64
            )
            SELECT instance_id
            FROM lineage
            ORDER BY depth
            """,
            instance_id,
        )
        lineage_instance_ids = tuple(row["instance_id"] for row in instance_lineage) or (instance_id,)

        scene_ids = {state["entity_id"]}
        if state["location_entity_id"]:
            scene_ids.add(state["location_entity_id"])
            colocated = await self.db.fetch(
                """
                SELECT subject_entity_id AS entity_id
                FROM aios.world_entity_relation
                WHERE world_id=$1
                  AND relation_type='located_in'
                  AND object_entity_id=$2
                  AND valid_to_node_id IS NULL
                """,
                state["world_id"],
                state["location_entity_id"],
            )
            scene_ids.update(row["entity_id"] for row in colocated if row["entity_id"])

        return HUDContext(
            instance_id=instance_id,
            character_id=state["character_id"],
            entity_id=state["entity_id"],
            world_id=state["world_id"],
            world_key=state["world_key"],
            timeline_id=state["timeline_id"],
            head_node_id=state["head_node_id"],
            source_timeline_id=state["source_timeline_id"],
            source_head_node_id=state["source_head_node_id"],
            state_version=state["state_version"],
            lifecycle_state=state["lifecycle_state"],
            location_entity_id=state["location_entity_id"],
            lineage_world_ids=lineage_world_ids,
            lineage_instance_ids=lineage_instance_ids,
            scene_entity_ids=frozenset(scene_ids),
        )
