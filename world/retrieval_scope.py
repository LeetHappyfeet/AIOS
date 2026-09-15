from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.world.resolution import get_effective_world_scope, normalize_domain


@dataclass(frozen=True)
class WorldScopeStage:
    priority: int
    world_ids: tuple[UUID, ...]

    def qdrant_world_ids(self) -> tuple[str, ...]:
        return tuple(str(world_id) for world_id in self.world_ids)


@dataclass(frozen=True)
class RetrievalScope:
    world_id: UUID
    domain: str
    stages: tuple[WorldScopeStage, ...]

    @property
    def all_world_ids(self) -> tuple[UUID, ...]:
        return tuple(
            world_id
            for stage in self.stages
            for world_id in stage.world_ids
        )

    @property
    def qdrant_world_stages(self) -> tuple[tuple[str, ...], ...]:
        return tuple(stage.qdrant_world_ids() for stage in self.stages)


async def build_retrieval_scope(
    db: Database,
    *,
    world_id: UUID,
    domain: str,
) -> RetrievalScope:
    """Build an ordered retrieval allow-list from the materialized world cache.

    This function performs exactly one database lookup. It does not recurse over
    world relations and it does not query RDF. Rows are grouped by materialized
    priority so callers can search the local canon first and lazily fall back to
    inherited worlds only when needed.
    """
    normalized_domain = normalize_domain(domain)
    rows = await get_effective_world_scope(
        db,
        world_id=world_id,
        domain=normalized_domain,
    )

    grouped: dict[int, list[UUID]] = {}
    for row in rows:
        source_world_id = row.get("source_world_id")
        if source_world_id is None:
            continue
        priority = int(row.get("priority") or 0)
        grouped.setdefault(priority, []).append(source_world_id)

    # The current world is always a legal local source even if a stale cache is
    # temporarily missing its self row. Keeping this invariant here prevents a
    # retrieval outage while still refusing to widen into unrelated worlds.
    if not any(world_id in values for values in grouped.values()):
        grouped.setdefault(0, []).insert(0, world_id)

    stages: list[WorldScopeStage] = []
    seen: set[UUID] = set()
    for priority in sorted(grouped):
        values: list[UUID] = []
        for candidate in grouped[priority]:
            if candidate in seen:
                continue
            seen.add(candidate)
            values.append(candidate)
        if values:
            stages.append(WorldScopeStage(priority=priority, world_ids=tuple(values)))

    if not stages:
        stages.append(WorldScopeStage(priority=0, world_ids=(world_id,)))

    return RetrievalScope(
        world_id=world_id,
        domain=normalized_domain,
        stages=tuple(stages),
    )


__all__ = ["RetrievalScope", "WorldScopeStage", "build_retrieval_scope"]
