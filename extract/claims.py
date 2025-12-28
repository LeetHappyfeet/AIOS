# aios_app/extract/claims.py

from __future__ import annotations

import logging
import re
from typing import Optional
from uuid import UUID

from ..db import Database

logger = logging.getLogger("aios.extract.claims")

EXTRACTION_VERSION = "v0.1.0"

# ---------------------------------------------------------------------
# Claim patterns (VERY conservative)
# ---------------------------------------------------------------------

IS_A_PATTERN = re.compile(
    r"""
    ^\s*
    (?P<subject>[A-Z][A-Za-z0-9_\- ]+?)
    \s+
    (is|are|was|were)
    \s+
    (?P<object>.+?)
    \s*$
    """,
    re.VERBOSE,
)

HAS_PATTERN = re.compile(
    r"""
    ^\s*
    (?P<subject>[A-Z][A-Za-z0-9_\- ]+?)
    \s+
    (has|have|had)
    \s+
    (?P<object>.+?)
    \s*$
    """,
    re.VERBOSE,
)

LOCATED_PATTERN = re.compile(
    r"""
    ^\s*
    (?P<subject>[A-Z][A-Za-z0-9_\- ]+?)
    \s+
    (is|was)
    \s+
    (in|inside|at|on)
    \s+
    (?P<object>.+?)
    \s*$
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _normalize(text: str) -> str:
    return text.strip().rstrip(".!?")

def _base_confidence(rule: str) -> float:
    """
    Conservative defaults.
    Dialogue and narration are mixed at this stage.
    """
    return {
        "is_a": 0.55,
        "has": 0.50,
        "located": 0.45,
    }.get(rule, 0.3)


# ---------------------------------------------------------------------
# Pipeline job handler
# ---------------------------------------------------------------------

def handle_extract_claims(db: Database, payload: dict) -> None:
    """
    Pipeline job:
    - Reads extracted_sentence
    - Writes claim_candidate
    - NEVER deletes or overwrites
    """

    document_id: Optional[UUID] = payload.get("document_id")

    if not document_id:
        raise ValueError("payload missing document_id")

    logger.info("Extracting claim candidates from document %s", document_id)

    sentences = db.fetch(
        """
        SELECT
            es.sentence_id,
            es.sentence_text
        FROM aios.extracted_sentence es
        JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        WHERE ds.document_id = $1
        ORDER BY es.sentence_id
        """,
        document_id,
    )

    inserted = 0

    for row in sentences:
        sentence_id = row["sentence_id"]
        text = _normalize(row["sentence_text"])

        if not text:
            continue

        m = IS_A_PATTERN.match(text)
        if m:
            _insert_claim(
                db,
                sentence_id,
                subject=m.group("subject"),
                predicate="is",
                obj=m.group("object"),
                raw=text,
                confidence=_base_confidence("is_a"),
                rule="is_a",
            )
            inserted += 1
            continue

        m = HAS_PATTERN.match(text)
        if m:
            _insert_claim(
                db,
                sentence_id,
                subject=m.group("subject"),
                predicate="has",
                obj=m.group("object"),
                raw=text,
                confidence=_base_confidence("has"),
                rule="has",
            )
            inserted += 1
            continue

        m = LOCATED_PATTERN.match(text)
        if m:
            _insert_claim(
                db,
                sentence_id,
                subject=m.group("subject"),
                predicate="located_in",
                obj=m.group("object"),
                raw=text,
                confidence=_base_confidence("located"),
                rule="located",
            )
            inserted += 1
            continue

    logger.info(
        "Extracted %s claim candidates from document %s",
        inserted,
        document_id,
    )


# ---------------------------------------------------------------------
# Insert helper
# ---------------------------------------------------------------------

def _insert_claim(
    db: Database,
    sentence_id: UUID,
    *,
    subject: str,
    predicate: str,
    obj: Optional[str],
    raw: str,
    confidence: float,
    rule: str,
) -> None:
    """
    Inserts a single claim_candidate row.
    """

    db.execute(
        """
        INSERT INTO aios.claim_candidate (
            sentence_id,
            subject,
            predicate,
            object,
            raw_text,
            confidence,
            extraction_rule,
            extraction_ver
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        sentence_id,
        subject.strip(),
        predicate,
        obj.strip() if obj else None,
        raw,
        confidence,
        rule,
        EXTRACTION_VERSION,
    )
