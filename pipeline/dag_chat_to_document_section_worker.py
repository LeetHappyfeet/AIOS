# aios_app/pipeline/dag_chat_to_document_section_worker.py

from __future__ import annotations
import logging
from aios_app.db import Database

logger = logging.getLogger("aios.pipeline.dag_chat_to_document_section")


async def run_worker(db: Database, *, node_id: str) -> None:
    row = await db.fetchrow(
        """
        SELECT
            dn.node_id,
            dn.message_text,
            dn.payload
        FROM aios.dag_node dn
        WHERE dn.node_id = $1
        """,
        node_id,
    )

    if not row or not row["message_text"]:
        return

    payload = row["payload"] or {}

    await db.execute(
        """
        INSERT INTO aios.document_section (
            document_id,
            node_id,
            section_path,
            section_order,
            content,
            meta
        )
        VALUES (
            NULL,
            $1,
            '/message',
            0,
            $2,
            $3::jsonb
        )
        ON CONFLICT (node_id) DO NOTHING
        """,
        row["node_id"],
        row["message_text"],
        payload,
    )

    logger.info("Projected chat node %s into document_section", row["node_id"])
