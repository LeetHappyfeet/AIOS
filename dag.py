from __future__ import annotations

from uuid import UUID
from typing import Optional, Dict, Any, Tuple
import json

from .db import Database

# -------------------------------------------------
# World helpers
# -------------------------------------------------

async def get_or_create_world(
    db: Database,
    *,
    world_key: str,
) -> UUID:
    """
    Resolve a symbolic world_key (e.g. 'liminal') to a concrete world_id.
    Worlds are singleton by key.
    """

    row = await db.fetchrow(
        """
        SELECT world_id
        FROM aios.world
        WHERE world_key = $1
        LIMIT 1
        """,
        world_key,
    )
    if row:
        return row["world_id"]

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.world (world_key, meta)
        VALUES ($1, '{}'::jsonb)
        ON CONFLICT (world_key) DO NOTHING
        RETURNING world_id
        """,
        world_key,
    )
    if row:
        return row["world_id"]

    row = await db.fetchrow(
        """
        SELECT world_id
        FROM aios.world
        WHERE world_key = $1
        LIMIT 1
        """,
        world_key,
    )
    return row["world_id"]


# -------------------------------------------------
# Timeline helpers
# -------------------------------------------------

async def get_or_create_timeline(
    db: Database,
    *,
    world_key: str,
    session_id: Optional[UUID],
    character_id: Optional[str],
    user_name: Optional[str],
    scope_key: str,
    meta: Optional[Dict[str, Any]] = None,
    source_id: Optional[str] = None,
) -> UUID:
    """
    Resolve the temporal DAG timeline for one conversation/source scope.

    Timeline identity is:
      world + name + session + character + user + scope

    The previous world/name-only identity caused every liminal conversation to
    share one DAG and interleave unrelated message histories.
    """

    meta_json = json.dumps(meta or {})
    world_id = await get_or_create_world(db, world_key=world_key)

    async def _fetch_existing():
        return await db.fetchrow(
            """
            SELECT timeline_id
            FROM aios.timeline
            WHERE world_id = $1
              AND name = 'main'
              AND session_id IS NOT DISTINCT FROM $2
              AND character_id IS NOT DISTINCT FROM $3
              AND user_name IS NOT DISTINCT FROM $4
              AND scope_key = $5
              AND source_id IS NOT DISTINCT FROM $6
            LIMIT 1
            """,
            world_id,
            session_id,
            character_id,
            user_name,
            scope_key,
            source_id,
        )

    row = await _fetch_existing()
    if row:
        return row["timeline_id"]

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.timeline (
            world_id,
            name,
            session_id,
            character_id,
            user_name,
            scope_key,
            meta,
            source_id
        )
        VALUES ($1, 'main', $2, $3, $4, $5, $6::jsonb, $7)
        ON CONFLICT DO NOTHING
        RETURNING timeline_id
        """,
        world_id,
        session_id,
        character_id,
        user_name,
        scope_key,
        meta_json,
        source_id,
    )
    if row:
        return row["timeline_id"]

    row = await _fetch_existing()
    if not row:
        raise RuntimeError(
            "Timeline insert conflicted but matching timeline could not be resolved"
        )
    return row["timeline_id"]


# -------------------------------------------------
# DAG node helpers
# -------------------------------------------------

async def get_previous_node_id(
    db: Database,
    *,
    timeline_id: UUID,
    event_id: int,
) -> Optional[UUID]:
    """
    Return the most recent DAG node whose ingest event precedes event_id.

    event_id, rather than created_at, is the deterministic ingestion-order
    anchor. The node's event_time preserves the source/arrival timestamp.
    """
    row = await db.fetchrow(
        """
        SELECT node_id
        FROM aios.dag_node
        WHERE timeline_id = $1
          AND event_id < $2
        ORDER BY event_id DESC
        LIMIT 1
        """,
        timeline_id,
        event_id,
    )
    return row["node_id"] if row else None


async def add_node_and_edge(
    db: Database,
    *,
    timeline_id: UUID,
    event_id: int,
    character_id: Optional[str],
    kind: str,
    speaker_id: Optional[str],
    speaker_role: Optional[str],
    recipient_id: Optional[str],
    message_text: Optional[str],
    payload: Dict[str, Any],
    viewpoint_id: Optional[str] = None,
    parent_node_id: Optional[UUID] = None,
    edge_type: str = "next",
) -> Tuple[UUID, Optional[UUID]]:
    """
    Insert a DAG node and optionally attach it to a parent.

    Structural rules:
    - document nodes are roots
    - explicit parents are honored for document-derived child nodes
    - chat nodes chain to the preceding ingest event in the timeline
    - event_time is copied from ingest_event and is the temporal timestamp
    """

    payload_json = json.dumps(payload or {})

    # Resolve the implicit parent BEFORE relying on the newly inserted node.
    # Using event_id < current event_id prevents the old self-parent bug even
    # if timestamps collide.
    parent: Optional[UUID]
    if kind == "document":
        parent = None
    elif parent_node_id is not None:
        parent = parent_node_id
    else:
        parent = await get_previous_node_id(
            db,
            timeline_id=timeline_id,
            event_id=event_id,
        )

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.dag_node (
            timeline_id,
            event_id,
            event_time,
            character_id,
            kind,
            speaker_id,
            speaker_role,
            recipient_id,
            viewpoint_id,
            message_text,
            payload
        )
        SELECT
            $1,
            $2,
            COALESCE(ie.event_time, ie.created_at),
            $3,
            $4::aios.event_kind,
            $5,
            $6::aios.actor_type,
            $7,
            $8,
            $9,
            $10::jsonb
        FROM aios.ingest_event ie
        WHERE ie.event_id = $2
        ON CONFLICT (timeline_id, event_id) DO NOTHING
        RETURNING node_id
        """,
        timeline_id,
        event_id,
        character_id,
        kind,
        speaker_id,
        speaker_role,
        recipient_id,
        viewpoint_id,
        message_text,
        payload_json,
    )

    if row:
        node_id = row["node_id"]
    else:
        row = await db.fetchrow(
            """
            SELECT node_id
            FROM aios.dag_node
            WHERE timeline_id = $1
              AND event_id = $2
            """,
            timeline_id,
            event_id,
        )
        if not row:
            raise RuntimeError(
                f"DAG node could not be inserted or resolved for event_id={event_id}"
            )
        node_id = row["node_id"]

    if parent and parent != node_id:
        await db.execute(
            """
            INSERT INTO aios.dag_edge (
                timeline_id,
                parent_node_id,
                child_node_id,
                edge_type,
                meta
            )
            VALUES ($1, $2, $3, $4, '{}'::jsonb)
            ON CONFLICT DO NOTHING
            """,
            timeline_id,
            parent,
            node_id,
            edge_type,
        )

    # DAG persistence is the first pipeline latch. Idempotent replay must not
    # reopen an event that already completed RDF processing.
    await db.execute(
        """
        UPDATE aios.ingest_event
        SET dag_processed_at = COALESCE(dag_processed_at, now()),
            process_status = CASE
                WHEN rdf_processed_at IS NOT NULL THEN 'done'::aios.process_status
                WHEN $2::aios.event_kind = 'document' THEN 'done'::aios.process_status
                ELSE 'processing'::aios.process_status
            END,
            processed_at = CASE
                WHEN rdf_processed_at IS NOT NULL THEN processed_at
                WHEN $2::aios.event_kind = 'document' THEN COALESCE(processed_at, now())
                ELSE processed_at
            END,
            process_error = CASE
                WHEN rdf_processed_at IS NOT NULL THEN process_error
                ELSE NULL
            END
        WHERE event_id = $1
        """,
        event_id,
        kind,
    )

    return node_id, parent
