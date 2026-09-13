from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import UUID

from aios_app.db import Database

_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def normalize_domain(domain: str) -> str:
    value = str(domain).strip().lower()
    if not _DOMAIN_RE.fullmatch(value):
        raise ValueError("domain must match ^[a-z][a-z0-9_]{0,31}$")
    return value


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
    """Build a staged world allow-list without importing the world runtime package.

    The database function reads the already-materialized resolution cache, so
    HUD retrieval performs one indexed SQL lookup and no recursive traversal.
    """
    normalized_domain = normalize_domain(domain)
    rows = await db.fetch(
        """
        SELECT source_world_id, priority, depth
        FROM aios.get_world_resolution_scope($1, $2)
        """,
        world_id,
        normalized_domain,
    )

    grouped: dict[int, list[UUID]] = {}
    for row in rows:
        source_world_id = row.get("source_world_id")
        if source_world_id is None:
            continue
        priority = int(row.get("priority") or 0)
        grouped.setdefault(priority, []).append(source_world_id)

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


__all__ = [
    "RetrievalScope",
    "WorldScopeStage",
    "build_retrieval_scope",
    "normalize_domain",
]
