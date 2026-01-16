from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from aios_app.db import Database


@dataclass(frozen=True)
class IndexRow:
    section_id: UUID
    qdrant_collection: str
    embedding_model: str
    embedding_version: str
    vector_hash: str | None
    last_error: str | None


async def get_index_state(
    db: Database,
    *,
    section_id: UUID,
    qdrant_collection: str,
    embedding_model: str,
    embedding_version: str,
) -> Optional[IndexRow]:
    """
    Return index state for the specific (collection, model, version).
    With composite PK, there may be multiple rows per section_id.
    """
    row = await db.fetchrow(
        """
        SELECT
          section_id,
          qdrant_collection,
          embedding_model,
          embedding_version,
          vector_hash,
          last_error
        FROM aios.vector_index_state
        WHERE section_id = $1
          AND qdrant_collection = $2
          AND embedding_model = $3
          AND embedding_version = $4
        """,
        section_id,
        qdrant_collection,
        embedding_model,
        embedding_version,
    )
    if not row:
        return None

    return IndexRow(
        section_id=row["section_id"],
        qdrant_collection=row["qdrant_collection"],
        embedding_model=row["embedding_model"],
        embedding_version=row["embedding_version"],
        vector_hash=row["vector_hash"],
        last_error=row["last_error"],
    )


async def mark_indexed(
    db: Database,
    *,
    section_id: UUID,
    qdrant_collection: str,
    embedding_model: str,
    embedding_version: str,
    vector_hash: str | None,
) -> None:
    await db.execute(
        """
        INSERT INTO aios.vector_index_state (
          section_id,
          qdrant_collection,
          embedding_model,
          embedding_version,
          vector_hash,
          indexed_at,
          last_error
        )
        VALUES ($1, $2, $3, $4, $5, now(), NULL)
        ON CONFLICT (section_id, qdrant_collection, embedding_model, embedding_version)
        DO UPDATE SET
          vector_hash = EXCLUDED.vector_hash,
          indexed_at = now(),
          last_error = NULL
        """,
        section_id,
        qdrant_collection,
        embedding_model,
        embedding_version,
        vector_hash,
    )


async def mark_error(
    db: Database,
    *,
    section_id: UUID,
    qdrant_collection: str,
    embedding_model: str,
    embedding_version: str,
    error: str,
) -> None:
    await db.execute(
        """
        INSERT INTO aios.vector_index_state (
          section_id,
          qdrant_collection,
          embedding_model,
          embedding_version,
          indexed_at,
          last_error
        )
        VALUES ($1, $2, $3, $4, now(), $5)
        ON CONFLICT (section_id, qdrant_collection, embedding_model, embedding_version)
        DO UPDATE SET
          indexed_at = now(),
          last_error = EXCLUDED.last_error
        """,
        section_id,
        qdrant_collection,
        embedding_model,
        embedding_version,
        error[:2000],
    )
