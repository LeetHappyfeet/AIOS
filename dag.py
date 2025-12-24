# =================================================
# dag.py — timeline + DAG helpers (FIXED)
# =================================================

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
    session_id: UUID,
    character_id: str,
    user_name: Optional[str],
    scope_key: str,
    meta: Optional[Dict[str, Any]] = None,
) -> UUID:
    """
    One timeline per (session, character, user, scope).
    """

    meta_json = json.dumps(meta or {})

    row = await db.fetchrow(
        """
        SELECT timeline_id
        FROM aios.timeline
        WHERE session_id = $1
          AND character_id = $2
          AND user_name IS NOT DISTINCT FROM $3
          AND scope_key = $4
        ORDER BY created_at DESC
        LIMIT 1
        """,
        session_id,
        character_id,
        user_name,
        scope_key,
    )

    if row:
        return row["timeline_id"]

    created = await db.execute_returning_row(
        """
        INSERT INTO aios.timeline (
            session_id,
            character_id,
            user_name,
            scope_key,
            meta
        )
        VALUES ($1, $2, $3, $4, $5::jsonb)
        RETURNING timeline_id
        """,
        session_id,
        character_id,
        user_name,
        scope_key,
        meta_json,
    )

    return created["timeline_id"]

# -------------------------------------------------
# DAG node helpers
# -------------------------------------------------

async def get_last_node_id(
    db: Database,
    timeline_id: UUID,
) -> Optional[UUID]:
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
    edge_type: str = "next",
) -> Tuple[UUID, Optional[UUID]]:
    """
    Insert a DAG node and sequential edge.
    """

    payload_json = json.dumps(payload or {})

    parent_node_id = await get_last_node_id(db, timeline_id)

    node_row = await db.execute_returning_row(
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

    node_id = node_row["node_id"]

    if parent_node_id:
        await db.execute(
            """
            INSERT INTO aios.dag_edge (
                timeline_id,
                parent_node_id,
                child_node_id,
                meta
            )
            VALUES ($1, $2, $3, '{}'::jsonb)
            ON CONFLICT DO NOTHING
            """,
            timeline_id,
            parent_node_id,
            node_id,
        )

    return node_id, parent_node_id
