# aios_app/pipeline/dag_to_document_section_worker.py

from __future__ import annotations

import asyncio
import logging

from aios_app.db import Database
from aios_app.config import settings

logger = logging.getLogger("aios.pipeline.dag_to_document_section")


# =================================================
# Worker
# =================================================

async def run_dag_to_document_section_worker(db: Database) -> None:
    logger.info("🔍 Starting DAG → document_section worker")

    # -------------------------------------------------
    # Sanity: confirm DB connection
    # -------------------------------------------------
    db_row = await db.fetchrow("SELECT current_database() AS db")
    logger.info("Connected to database: %s", db_row["db"])

    # -------------------------------------------------
    # Preflight diagnostics
    # -------------------------------------------------
    eligible = await db.fetchrow(
        """
        SELECT COUNT(*) AS cnt
        FROM aios.dag_node p
        WHERE p.kind = 'paragraph'
          AND p.payload ? 'document_id'
          AND p.payload ? 'paragraph_index'
        """
    )

    logger.info(
        "Paragraph nodes with document_id + paragraph_index: %d",
        eligible["cnt"],
    )

    # -------------------------------------------------
    # Select unprojected paragraphs
    # -------------------------------------------------
    rows = await db.fetch(
        """
        SELECT
            p.node_id                                   AS paragraph_node_id,
            (p.payload->>'document_id')::uuid          AS document_id,
            (p.payload->>'paragraph_index')::int       AS paragraph_index,
            p.message_text                             AS content
        FROM aios.dag_node p
        WHERE p.kind = 'paragraph'
          AND p.payload ? 'document_id'
          AND p.payload ? 'paragraph_index'
          AND NOT EXISTS (
              SELECT 1
              FROM aios.document_section ds
              WHERE ds.node_id = p.node_id
          )
        ORDER BY
            (p.payload->>'document_id')::uuid,
            (p.payload->>'paragraph_index')::int
        LIMIT 100
        """
    )

    logger.info("Found %d paragraphs to project", len(rows))

    if not rows:
        logger.warning("⚠️ No rows selected — nothing to do")
        return

    # -------------------------------------------------
    # Insert loop
    # -------------------------------------------------
    inserted = 0

    for row in rows:
        logger.debug(
            "Projecting paragraph node=%s document=%s index=%s",
            row["paragraph_node_id"],
            row["document_id"],
            row["paragraph_index"],
        )

        status = await db.execute(
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
            row["document_id"],
            row["paragraph_node_id"],
            f"/paragraph/{row['paragraph_index']}",
            row["paragraph_index"],
            row["content"],
        )

        # asyncpg returns strings like: "INSERT 0 1" or "INSERT 0 0"
        if status.endswith("1"):
            inserted += 1
        else:
            logger.warning(
                "Insert skipped (conflict) for node %s",
                row["paragraph_node_id"],
            )

    logger.info(
        "✅ dag_to_document_section complete — inserted %d / %d rows",
        inserted,
        len(rows),
    )


# =================================================
# CLI entrypoint (SAFE)
# =================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main() -> None:
        db = Database(settings.db_dsn)
        await db.connect()
        try:
            await run_dag_to_document_section_worker(db)
        finally:
            await db.close()

    asyncio.run(main())
