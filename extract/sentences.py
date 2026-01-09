# aios_app/extract/sentences.py

from __future__ import annotations

import logging
import re
from typing import List
from uuid import UUID

from aios_app.db import Database

logger = logging.getLogger("aios.extract.sentences")

# =================================================
# Sentence boundary detection (deterministic)
# =================================================

SENTENCE_BOUNDARY = re.compile(
    r"""
    (?<!\bMr)
    (?<!\bMrs)
    (?<!\bMs)
    (?<!\bDr)
    (?<!\bProf)
    (?<!\bSr)
    (?<!\bJr)
    (?<!\betc)
    (?<=[.!?])
    \s+
    """,
    re.VERBOSE,
)

def split_sentences(text: str) -> List[str]:
    """
    Deterministically split text into sentences.

    No semantic interpretation.
    No NLP.
    Stable across runs.
    """
    text = (text or "").strip()
    if not text:
        return []

    parts = SENTENCE_BOUNDARY.split(text)
    return [p.strip() for p in parts if p.strip()]

# =================================================
# Worker
# =================================================

async def run_sentence_extraction_worker(db: Database) -> None:
    """
    DAG → document_section → extracted_sentence

    Processes ONLY document_sections that have not yet
    produced extracted_sentence rows.
    """

    logger.info("Starting sentence extraction worker")

    rows = await db.fetch(
        """
        SELECT
            ds.section_id,
            ds.content
        FROM aios.document_section ds
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.extracted_sentence es
            WHERE es.section_id = ds.section_id
        )
        ORDER BY ds.section_order
        """
    )

    logger.info("Found %d document_sections to process", len(rows))

    for row in rows:
        section_id: UUID = row["section_id"]
        content: str = row["content"]

        try:
            await _process_section(
                db=db,
                section_id=section_id,
                content=content,
            )
        except Exception:
            logger.exception(
                "Failed extracting sentences for section %s",
                section_id,
            )

    logger.info("Sentence extraction worker complete")

# =================================================
# Section processing
# =================================================

async def _process_section(
    *,
    db: Database,
    section_id: UUID,
    content: str,
) -> None:
    """
    Insert extracted_sentence rows for a single document_section.
    """

    sentences = split_sentences(content)

    logger.debug(
        "Section %s → %d sentences",
        section_id,
        len(sentences),
    )

    for idx, sentence in enumerate(sentences):
        await db.execute(
            """
            INSERT INTO aios.extracted_sentence (
                section_id,
                sentence_index,
                sentence_text,
                meta
            )
            VALUES ($1, $2, $3, '{}'::jsonb)
            ON CONFLICT DO NOTHING
            """,
            section_id,
            idx,
            sentence,
        )

# =================================================
# CLI (optional standalone run)
# =================================================

if __name__ == "__main__":
    import asyncio
    from aios_app.config import settings

    logging.basicConfig(level=logging.INFO)

    async def main():
        db = Database(settings.db_dsn)
        await db.connect()
        try:
            await run_sentence_extraction_worker(db)
        finally:
            await db.close()

    asyncio.run(main())
