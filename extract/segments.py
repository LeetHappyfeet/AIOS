# aios_app/extract/segments.py

from __future__ import annotations

import re
import logging
from typing import List, Tuple
from uuid import UUID

from ..db import Database

logger = logging.getLogger("aios.extract.segments")


# -------------------------------------------------
# Segment classification
# -------------------------------------------------

SEGMENT_NARRATION = "narration"
SEGMENT_DIALOGUE = "dialogue"
SEGMENT_META = "meta"


def classify_segment(text: str) -> str:
    """
    Classify a chunk of text into a semantic mode.

    Rules:
    - *italicized* blocks → narration
    - quoted text → dialogue
    - otherwise → dialogue (RP default)
    """
    t = text.strip()

    if not t:
        return SEGMENT_META

    # *action / narration*
    if t.startswith("*") and t.endswith("*"):
        return SEGMENT_NARRATION

    # quoted dialogue
    if t.startswith('"') and t.endswith('"'):
        return SEGMENT_DIALOGUE

    # fallback: dialogue
    return SEGMENT_DIALOGUE


# -------------------------------------------------
# Text segmentation
# -------------------------------------------------

ITALIC_BLOCK_RE = re.compile(r"\*[^*]+\*")
QUOTE_BLOCK_RE = re.compile(r'"[^"]+"')


def split_into_blocks(text: str) -> List[str]:
    """
    Splits RP-style text into semantic blocks while
    preserving original ordering.

    Handles:
    - *italicized narration*
    - quoted dialogue
    - free text between them
    """
    blocks: List[Tuple[int, int, str]] = []

    for match in ITALIC_BLOCK_RE.finditer(text):
        blocks.append((match.start(), match.end(), match.group()))

    for match in QUOTE_BLOCK_RE.finditer(text):
        blocks.append((match.start(), match.end(), match.group()))

    # sort by appearance
    blocks.sort(key=lambda x: x[0])

    result: List[str] = []
    cursor = 0

    for start, end, content in blocks:
        if cursor < start:
            gap = text[cursor:start].strip()
            if gap:
                result.append(gap)
        result.append(content.strip())
        cursor = end

    # trailing text
    if cursor < len(text):
        tail = text[cursor:].strip()
        if tail:
            result.append(tail)

    return result


# -------------------------------------------------
# Pipeline handler
# -------------------------------------------------

def handle_split_sections(db: Database, payload: dict) -> None:
    """
    Pipeline job: split source_document into document_sections.

    Payload:
      {
        "document_id": "<uuid>"
      }
    """
    document_id = UUID(payload["document_id"])

    row = db.fetchrow_sync(
        """
        SELECT raw_content
        FROM aios.source_document
        WHERE document_id = $1
        """,
        document_id,
    )

    if not row:
        raise RuntimeError(f"source_document not found: {document_id}")

    raw_text = row["raw_content"]

    blocks = split_into_blocks(raw_text)

    logger.info(
        "Splitting document %s into %d segments",
        document_id,
        len(blocks),
    )

    section_order = 0

    for block in blocks:
        section_type = classify_segment(block)

        section_path = f"segment:{section_order}:{section_type}"

        db.execute_sync(
            """
            INSERT INTO aios.document_section (
                document_id,
                section_path,
                section_order,
                content
            )
            VALUES ($1, $2, $3, $4)
            """,
            document_id,
            section_path,
            section_order,
            block,
        )

        section_order += 1