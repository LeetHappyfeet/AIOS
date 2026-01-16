# aios_app/pipeline/dag_to_document_section_worker.py

from __future__ import annotations

import logging
from uuid import UUID

from aios_app.db import Database

logger = logging.getLogger("aios.pipeline.dag_to_document_section")


async def run_worker(
    db: Database,
    *,
    node_id: UUID,
) -> None:
    """
    Project ONE DAG node into document_section.

    Supervisor guarantees eligibility.
    This worker executes unconditionally.
    """

    row = await db.fetchrow(
        """
        SELECT
            n.node_id,
            n.kind,
            n.event_id,
            n.message_text AS content,
            (n.payload->>'document_id')::uuid    AS document_id,
            (n.payload->>'paragraph_index')::int AS paragraph_index
        FROM aios.dag_node n
        WHERE n.node_id = $1
        """,
        node_id,
    )

    if not row:
        # Node was deleted or race condition — safe to ignore
        logger.warning("Node %s disappeared before projection", node_id)
        return

    if row["kind"] == "paragraph":
        section_path = f"/paragraph/{row['paragraph_index']}"
        section_order = row["paragraph_index"]
        document_id = row["document_id"]

    elif row["kind"] == "chat_message":
        section_path = f"/chat/{row['event_id']}"
        section_order = row["event_id"]
        document_id = None

    else:
        # Supervisor should never schedule this
        raise RuntimeError(
            f"Unsupported dag_node.kind {row['kind']!r} for node {node_id}"
        )

    await db.execute(
        """
        INSERT INTO aios.document_section (
            document_id,
            node_id,
            section_path,
            section_order,
            content
        )
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (node_id) DO NOTHING
        """,
        document_id,
        row["node_id"],
        section_path,
        section_order,
        row["content"],
    )

    logger.debug(
        "Projected %s node %s → document_section",
        row["kind"],
        node_id,
    )
