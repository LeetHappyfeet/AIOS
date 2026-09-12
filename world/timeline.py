from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from aios_app.db import Database


EXPOSURE_KINDS = frozenset({
    "observed",
    "participated",
    "caused",
    "heard_about",
    "inferred_from",
    "remembered_from",
    "sensor",
    "system",
})


async def ensure_objective_timeline(db: Database, *, world_id: UUID) -> UUID:
    """Return the one world-owned objective timeline for ``world_id``.

    The SQL function is the invariant owner because worlds/timelines can be
    created by ingestion paths other than WorldRuntimeService.  This wrapper is
    the application-level entry point for code that needs the shared coordinate.
    """
    row = await db.fetchrow(
        "SELECT aios.ensure_world_objective_timeline($1) AS timeline_id",
        world_id,
    )
    if not row or not row["timeline_id"]:
        raise RuntimeError(f"Could not resolve objective timeline for world {world_id}")
    return row["timeline_id"]


async def get_character_world_timeline_link(
    db: Database,
    *,
    character_timeline_id: UUID,
) -> Optional[dict[str, Any]]:
    """Return the objective world timeline associated with a subjective DAG."""
    row = await db.fetchrow(
        """
        SELECT character_timeline_id, world_id, world_timeline_id,
               relation, created_at, meta
        FROM aios.character_world_timeline_link
        WHERE character_timeline_id=$1
        """,
        character_timeline_id,
    )
    return dict(row) if row else None


async def record_world_event_exposure(
    db: Database,
    *,
    instance_id: UUID,
    character_timeline_id: UUID,
    world_node_id: UUID,
    exposure_kind: str,
    character_node_id: Optional[UUID] = None,
    acquisition_id: Optional[UUID] = None,
    confidence: Optional[float] = None,
    meta: Optional[dict[str, Any]] = None,
) -> UUID:
    """Link objective world history to a character's epistemic history.

    This does not copy a world fact into /char and does not assert belief.  It
    records only the exposure/provenance bridge.  Knowledge acquisition and
    belief reconciliation remain separate downstream operations.
    """
    if exposure_kind not in EXPOSURE_KINDS:
        raise ValueError(f"unsupported exposure_kind '{exposure_kind}'")
    if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("confidence must be between 0 and 1")

    link = await get_character_world_timeline_link(
        db,
        character_timeline_id=character_timeline_id,
    )
    if not link:
        raise ValueError(
            f"character timeline {character_timeline_id} is not linked to a world"
        )

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.world_event_exposure (
            instance_id,
            world_id,
            world_timeline_id,
            world_node_id,
            character_timeline_id,
            character_node_id,
            acquisition_id,
            exposure_kind,
            confidence,
            meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
        RETURNING exposure_id
        """,
        instance_id,
        link["world_id"],
        link["world_timeline_id"],
        world_node_id,
        character_timeline_id,
        character_node_id,
        acquisition_id,
        exposure_kind,
        confidence,
        json.dumps(meta or {}),
    )
    return row["exposure_id"]
