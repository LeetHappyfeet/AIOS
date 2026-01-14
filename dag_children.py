# aios_app/dag_children.py

from __future__ import annotations

import json
from typing import Optional, Dict, Any
from uuid import UUID

from .db import Database


async def add_child_node(
    db: Database,
    *,
    timeline_id: UUID,
    parent_node_id: UUID,
    event_id: int,
    kind: str,
    message_text: Optional[str],
    payload: Dict[str, Any],
    speaker_id: Optional[str] = None,
    speaker_role: Optional[str] = None,
    recipient_id: Optional[str] = None,
    edge_type: str = "next",
) -> UUID:
    """
    Insert a DAG node with an explicit parent.

    This bypasses timeline tail logic and guarantees
    correct parent → child structure.
    """

    payload_json = json.dumps(payload or {})

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
        parent_node_id,
        node_id,
        edge_type,
    )

    return node_id
