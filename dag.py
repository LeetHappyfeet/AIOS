# aios/dag.py

from __future__ import annotations

from uuid import UUID
from typing import Optional, Dict, Any, Tuple
import json

from .db import Database

# -------------------------------------------------
# Timeline helpers
# -------------------------------------------------

async def get_or_create_timeline(
    db: Database,
    *,
    world_id: UUID,
    session_id: Optional[UUID],
    character_id: Optional[str],
    user_name: Optional[str],
    scope_key: str,
    meta: Optional[Dict[str, Any]] = None,
) -> UUID:
    """
    One timeline per world.
    World continuity is singular.
    Matches UNIQUE (world_id, name).
    """

    meta_json = json.dumps(meta or {})

    # 1. Fetch if exists (fast path)
    row = await db.fetchrow(
        """
        SELECT timeline_id
        FROM aios.timeline
        WHERE world_id = $1
          AND name = 'main'
        LIMIT 1
        """,
        world_id,
    )

    if row:
        return row["timeline_id"]

    # 2. Attempt insert (race-safe)
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.timeline (
            world_id,
            name,
            session_id,
            character_id,
            user_name,
            scope_key,
            meta
        )
        VALUES ($1, 'main', $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT (world_id, name) DO NOTHING
        RETURNING timeline_id
        """,
        world_id,
        session_id,
        character_id,
        user_name,
        scope_key,
        meta_json,
    )

    if row:
        return row["timeline_id"]

    # 3. Fetch again if another process won the race
    row = await db.fetchrow(
        """
        SELECT timeline_id
        FROM aios.timeline
        WHERE world_id = $1
          AND name = 'main'
        LIMIT 1
        """,
        world_id,
    )

    return row["timeline_id"]


# -------------------------------------------------
# DAG node helpers
# -------------------------------------------------

async def get_last_node_id(
    db: Database,
    timeline_id: UUID,
) -> Optional[UUID]:
    """
    Returns the most recent node in the timeline.
    Used only for chat-style chaining.
    """
    row = await db.fetchrow(
        """
        SELECT node_id
        FROM aios.dag_node
        WHERE timeline_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        timeline_id,
    )
    return row["node_id"] if row else None


async def add_node_and_edge(
    db: Database,
    *,
    timeline_id: UUID,
    event_id: int,
    kind: str,
    speaker_id: Optional[str],
    speaker_role: Optional[str],
    recipient_id: Optional[str],
    message_text: Optional[str],
    payload: Dict[str, Any],
    parent_node_id: Optional[UUID] = None,
    edge_type: str = "next",
) -> Tuple[UUID, Optional[UUID]]:
    """
    Insert a DAG node and optionally attach it to a parent.

    Structural rules:
    - document nodes are ALWAYS roots (no parent)
    - paragraph / sentence nodes REQUIRE explicit parent
    - chat nodes chain to the previous node if no parent supplied
    """

    payload_json = json.dumps(payload or {})

    # -------------------------------------------------
    # 1. Insert node (idempotent)
    # -------------------------------------------------
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.dag_node (
            timeline_id,
            event_id,
            kind,
            speaker_id,
            speaker_role,
            recipient_id,
            message_text,
            payload
        )
        VALUES (
            $1,
            $2,
            $3::aios.event_kind,
            $4,
            $5::aios.actor_type,
            $6,
            $7,
            $8::jsonb
        )
        ON CONFLICT (timeline_id, event_id) DO NOTHING
        RETURNING node_id
        """,
        timeline_id,
        event_id,
        kind,
        speaker_id,
        speaker_role,
        recipient_id,
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
        node_id = row["node_id"]

    # -------------------------------------------------
    # 2. Determine parent
    # -------------------------------------------------
    parent: Optional[UUID] = None

    if kind == "document":
        parent = None  # documents are absolute roots

    elif parent_node_id is not None:
        parent = parent_node_id  # explicit structural parent

    else:
        # chat-style chaining only
        parent = await get_last_node_id(db, timeline_id)

    # -------------------------------------------------
    # 3. Insert edge (if valid)
    # -------------------------------------------------
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

    return node_id, parent
