from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from aios_app.db import Database

logger = logging.getLogger("aios.world.source_cursor")


async def advance_matching_runtime_source_cursor(
    db: Database,
    *,
    character_id: str,
    session_id: Optional[UUID],
    user_name: Optional[str],
    scope_key: str,
    source_timeline_id: UUID,
    source_head_node_id: UUID,
    source_head_event_id: int,
) -> list[UUID]:
    """Advance the authorized source-perception boundary for matching runtimes.

    Invariants enforced here:
      * the incoming source coordinate must be a node on a liminal timeline;
      * source and runtime identity must match character/session/user/scope;
      * an unbound runtime may bind to its first exact source timeline;
      * an already bound runtime may only advance on that same source timeline;
      * a legacy self-bound runtime may be repaired, but one liminal stream is
        never silently rebound to another liminal stream;
      * ordinary source progression is monotonic by ingest event id.

    Source perception belongs to character_runtime_state. The shared runtime
    world is objective/session topology and is never mutated to carry a
    per-user source cursor.
    """

    async with db.connection() as con:
        async with con.transaction():
            source = await con.fetchrow(
                """
                SELECT
                    t.timeline_id,
                    t.session_id,
                    t.character_id,
                    t.user_name,
                    t.scope_key,
                    w.world_key,
                    dn.node_id,
                    dn.event_id
                FROM aios.timeline t
                JOIN aios.world w ON w.world_id=t.world_id
                JOIN aios.dag_node dn
                  ON dn.timeline_id=t.timeline_id
                 AND dn.node_id=$2
                WHERE t.timeline_id=$1
                """,
                source_timeline_id,
                source_head_node_id,
            )
            if not source:
                raise RuntimeError(
                    "source cursor coordinate is invalid: node does not belong to timeline"
                )
            if source["world_key"] != "liminal":
                raise RuntimeError(
                    "source cursor coordinate is invalid: live source timeline is not liminal"
                )
            if source["character_id"] != character_id:
                raise RuntimeError("source cursor character identity mismatch")
            if source["session_id"] != session_id:
                raise RuntimeError("source cursor session identity mismatch")
            if source["user_name"] != user_name:
                raise RuntimeError("source cursor user identity mismatch")
            if source["scope_key"] != scope_key:
                raise RuntimeError("source cursor scope identity mismatch")
            if int(source["event_id"]) != int(source_head_event_id):
                raise RuntimeError("source cursor event identity mismatch")

            rows = await con.fetch(
                """
                SELECT
                    rs.instance_id,
                    rs.timeline_id AS runtime_timeline_id,
                    rs.source_timeline_id,
                    rs.source_head_node_id,
                    current_head.event_id AS current_source_event_id,
                    current_source_world.world_key AS current_source_world_key
                FROM aios.character_runtime_state rs
                JOIN aios.character_instance ci
                  ON ci.instance_id=rs.instance_id
                JOIN aios.timeline rt
                  ON rt.timeline_id=rs.timeline_id
                LEFT JOIN aios.timeline current_source
                  ON current_source.timeline_id=rs.source_timeline_id
                LEFT JOIN aios.world current_source_world
                  ON current_source_world.world_id=current_source.world_id
                LEFT JOIN aios.dag_node current_head
                  ON current_head.node_id=rs.source_head_node_id
                WHERE ci.character_id=$1
                  AND rt.session_id IS NOT DISTINCT FROM $2
                  AND rt.user_name IS NOT DISTINCT FROM $3
                  AND rt.scope_key=$4
                FOR UPDATE OF rs
                """,
                character_id,
                session_id,
                user_name,
                scope_key,
            )

            advanced: list[UUID] = []
            for row in rows:
                current_timeline_id = row["source_timeline_id"]
                current_head_node_id = row["source_head_node_id"]
                current_world_key = row["current_source_world_key"]

                unbound = current_timeline_id is None
                same_source = current_timeline_id == source_timeline_id
                legacy_self_bound = (
                    current_timeline_id is not None
                    and current_timeline_id == row["runtime_timeline_id"]
                    and current_world_key != "liminal"
                )

                if not (unbound or same_source or legacy_self_bound):
                    logger.warning(
                        "Refusing source cursor rebind for instance %s: %s -> %s",
                        row["instance_id"],
                        current_timeline_id,
                        source_timeline_id,
                    )
                    continue

                current_event_id = row["current_source_event_id"]
                if (
                    same_source
                    and current_head_node_id != source_head_node_id
                    and current_event_id is not None
                    and int(current_event_id) > int(source_head_event_id)
                ):
                    logger.debug(
                        "Ignoring stale source cursor advance for instance %s: event %s < %s",
                        row["instance_id"],
                        source_head_event_id,
                        current_event_id,
                    )
                    continue

                await con.execute(
                    """
                    UPDATE aios.character_runtime_state
                    SET source_timeline_id=$2,
                        source_head_node_id=$3,
                        updated_at=now()
                    WHERE instance_id=$1
                    """,
                    row["instance_id"],
                    source_timeline_id,
                    source_head_node_id,
                )
                advanced.append(row["instance_id"])

            return advanced
