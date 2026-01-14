from __future__ import annotations

import logging
from aios_app.db import Database

logger = logging.getLogger("aios.pipeline.dag_to_source_document")

async def run_dag_to_source_document_worker(db: Database) -> None:
    rows = await db.fetch(
        """
        SELECT DISTINCT
            (dn.payload->>'document_id')::uuid AS document_id,
            dn.payload->>'url' AS url,
            dn.payload->>'source' AS source
        FROM aios.dag_node dn
        WHERE dn.kind = 'document'
          AND dn.payload ? 'document_id'
          AND NOT EXISTS (
              SELECT 1
              FROM aios.source_document sd
              WHERE sd.document_id = (dn.payload->>'document_id')::uuid
          )
        """
    )

    logger.info("Found %d documents to project into source_document", len(rows))

    for row in rows:
        await db.execute(
            """
            INSERT INTO aios.source_document (
                document_id,
                source_type,
                source_uri
            )
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            row["document_id"],
            row["source"],
            row["url"],
        )

    logger.info("dag_to_source_document worker complete")
