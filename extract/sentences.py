# aios_app/extract/sentences.py

from __future__ import annotations

import logging
import re
from typing import List
from uuid import UUID

from ..db import Database

logger = logging.getLogger("aios.extract.sentences")

# ---------------------------------------------------------------------
# Sentence splitter
# ---------------------------------------------------------------------

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
    Does NOT attempt semantic interpretation.
    """
    text = text.strip()

    if not text:
        return []

    parts = SENTENCE_BOUNDARY.split(text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------
# Pipeline job handler
# ---------------------------------------------------------------------

def handle_split_sentences(db: Database, payload: dict) -> None:
    """
    Pipeline job:
    - Input: document_id
    - Reads document_section
    - Writes extracted_sentence
    """

    document_id: UUID = payload.get("document_id")
    citation: str | None = payload.get("citation")

    if not document_id:
        raise ValueError("payload missing document_id")

    logger.info("Splitting sections into sentences for document %s", document_id)

    sections = db.fetch(
        """
        SELECT section_id, content
        FROM aios.document_section
        WHERE document_id = $1
        ORDER BY section_order ASC
        """,
        document_id,
    )

    inserted = 0

    for section in sections:
        section_id = section["section_id"]
        content = section["content"]

        sentences = split_sentences(content)

        for idx, sentence in enumerate(sentences):
            db.execute(
                """
                INSERT INTO aios.extracted_sentence (
                    section_id,
                    sentence_index,
                    sentence_text,
                    citation,
                    meta
                )
                VALUES ($1, $2, $3, $4, '{}'::jsonb)
                ON CONFLICT DO NOTHING
                """,
                section_id,
                idx,
                sentence,
                citation,
            )
            inserted += 1

    logger.info(
        "Extracted %s sentences from document %s",
        inserted,
        document_id,
    )
