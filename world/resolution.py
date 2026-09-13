from __future__ import annotations

import json
import re
from typing import Any, Iterable
from uuid import UUID

from aios_app.db import Database


RELATION_KINDS = frozenset({"inherits", "derived_from", "counterpart_of"})
INHERITANCE_MODES = frozenset({"inherit", "local_only"})
# Deliberately broad, stable buckets. The database accepts additional domains,
# but callers should prefer these rather than proliferating labels.
COMMON_WORLD_DOMAINS = frozenset({
    "general",
    "geography",
    "biology",
    "chemistry",
    "physics",
    "history",
    "politics",
    "technology",
    "astronomy",
    "culture",
})
_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def normalize_domain(domain: str) -> str:
    value = str(domain).strip().lower()
    if not _DOMAIN_RE.fullmatch(value):
        raise ValueError("domain must match ^[a-z][a-z0-9_]{0,31}$")
    return value


def normalize_domains(domains: Iterable[str]) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(normalize_domain(d) for d in domains))
    if not 1 <= len(values) <= 16:
        raise ValueError("world relations require between 1 and 16 domains")
    return values


async def get_effective_world_scope(
    db: Database,
    *,
    world_id: UUID,
    domain: str,
) -> list[dict[str, Any]]:
    """Return the precomputed world search scope for a retrieval domain.

    This is intentionally safe for the live HUD path: PostgreSQL executes one
    indexed lookup against ``world_resolution_cache``. No recursive CTE, RDF
    traversal, LLM planner, or graph reasoning occurs here.
    """
    domain = normalize_domain(domain)
    rows = await db.fetch(
        """
        SELECT source_world_id, priority, depth
        FROM aios.get_world_resolution_scope($1, $2)
        """,
        world_id,
        domain,
    )
    return [dict(row) for row in rows]


async def get_effective_world_ids(
    db: Database,
    *,
    world_id: UUID,
    domain: str,
) -> tuple[UUID, ...]:
    """Compact helper for SQL/Qdrant allow-list construction."""
    scope = await get_effective_world_scope(db, world_id=world_id, domain=domain)
    return tuple(row["source_world_id"] for row in scope)


async def set_world_relation(
    db: Database,
    *,
    world_id: UUID,
    source_world_id: UUID,
    domains: Iterable[str],
    relation: str = "inherits",
    priority: int = 100,
    meta: dict[str, Any] | None = None,
) -> UUID:
    """Create/update a world relation and materialize its retrieval closure.

    Cache rebuilding happens in database triggers on this write path, never on
    HUD retrieval. ``source_world_id`` is a source of compatible facts, not an
    assertion that the two universes are ontologically identical.
    """
    if relation not in RELATION_KINDS:
        raise ValueError(f"unsupported world relation '{relation}'")
    normalized = normalize_domains(domains)
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.world_relation (
            world_id, source_world_id, relation, priority, domains, meta
        )
        VALUES ($1,$2,$3,$4,$5::text[],$6::jsonb)
        ON CONFLICT (world_id, source_world_id, relation) DO UPDATE
           SET priority=EXCLUDED.priority,
               domains=EXCLUDED.domains,
               enabled=true,
               updated_at=now(),
               meta=aios.world_relation.meta || EXCLUDED.meta
        RETURNING relation_id
        """,
        world_id,
        source_world_id,
        relation,
        int(priority),
        list(normalized),
        json.dumps(meta or {}),
    )
    return row["relation_id"]


async def set_domain_policy(
    db: Database,
    *,
    world_id: UUID,
    domain: str,
    inheritance_mode: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """Set a coarse inheritance boundary for a world/domain.

    ``local_only`` is the explicit canon boundary: it stops fallback beyond the
    world for that domain. This is how a setting can inherit mundane geography
    while refusing to assume real-world history or physics.
    """
    domain = normalize_domain(domain)
    if inheritance_mode not in INHERITANCE_MODES:
        raise ValueError(f"unsupported inheritance_mode '{inheritance_mode}'")
    await db.execute(
        """
        INSERT INTO aios.world_domain_policy (
            world_id, domain, inheritance_mode, meta
        )
        VALUES ($1,$2,$3,$4::jsonb)
        ON CONFLICT (world_id, domain) DO UPDATE
           SET inheritance_mode=EXCLUDED.inheritance_mode,
               updated_at=now(),
               meta=aios.world_domain_policy.meta || EXCLUDED.meta
        """,
        world_id,
        domain,
        inheritance_mode,
        json.dumps(meta or {}),
    )
