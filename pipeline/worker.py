#aios\pipeline\worker.py
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple
from uuid import UUID

import spacy

from aios_app.db import Database
from aios_app.config import settings

logger = logging.getLogger("aios.pipeline.worker")

# =================================================
# Configuration
# =================================================

LIMINAL_WORLD_KEY = "liminal"
WORKER_NAME = "claim_extractor"
WORKER_VERSION = "v4-spacy-sections"

# =================================================
# spaCy
# =================================================

NLP = spacy.load("en_core_web_sm", disable=["ner", "textcat"])


def split_sentences(text: str) -> List[str]:
    if not text:
        return []
    doc = NLP(text)
    return [s.text.strip() for s in doc.sents if s.text.strip()]


def extract_spo(sentence: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    doc = NLP(sentence)

    subject = predicate = obj = None
    root = doc[:].root

    # ---------------------------------
    # Case 1: Normal verbal predicate
    # ---------------------------------
    if root.pos_ == "VERB":
        predicate = root.lemma_

        for tok in doc:
            if subject is None and tok.dep_ in ("nsubj", "nsubjpass"):
                subject = tok.text

            if obj is None and tok.dep_ in ("dobj", "pobj", "attr"):
                obj = tok.text

        return subject, predicate, obj

    # ---------------------------------
    # Case 2: Copula clause (definition)
    # ---------------------------------
    has_copula = any(child.dep_ == "cop" for child in root.children)

    if has_copula:
        # Subject
        for tok in doc:
            if tok.dep_ in ("nsubj", "nsubjpass"):
                subject = tok.text
                break

        # Object = full predicate complement
        # (root + its modifiers)
        complement_tokens = [root] + list(root.subtree)
        complement_tokens = sorted(
            set(complement_tokens),
            key=lambda t: t.i
        )
        obj = " ".join(tok.text for tok in complement_tokens)

        predicate = "be_definition_of"

        return subject, predicate, obj

    # ---------------------------------
    # Fallback: unknown structure
    # ---------------------------------
    return None, None, None


# =================================================
# Worker
# =================================================

async def run_claim_extraction_worker(db: Database) -> None:
    logger.info("Starting claim extraction worker (document_section-based)")

    # -------------------------------------------------
    # Find document sections that have not been processed
    # -------------------------------------------------
    rows = await db.fetch(
        """
        SELECT
            ds.section_id,
            ds.document_id,
            ds.node_id,
            ds.section_order,
            ds.content
        FROM aios.document_section ds
        LEFT JOIN aios.extracted_sentence es
          ON es.section_id = ds.section_id
        WHERE es.sentence_id IS NULL
        ORDER BY ds.section_order
        """
    )

    logger.info("Found %d unprocessed document sections", len(rows))

    for row in rows:
        try:
            await _process_section(
                db=db,
                section_id=row["section_id"],
                document_id=row["document_id"],
                node_id=row["node_id"],
                content=row["content"],
            )
        except Exception:
            logger.exception("Failed processing section %s", row["section_id"])

    logger.info("Claim extraction worker complete")


# =================================================
# Section processing
# =================================================

async def _process_section(
    *,
    db: Database,
    section_id: UUID,
    document_id: Optional[UUID],
    node_id: Optional[UUID],
    content: str,
) -> None:
    """
    document_section → extracted_sentence → claim_candidate
    also writes:
      - claim_world_assignment (liminal)
      - claim_provenance (document-based)
    """

    sentences = split_sentences(content)

    logger.debug(
        "Section %s (doc=%s node=%s) → %d sentences",
        section_id,
        document_id,
        node_id,
        len(sentences),
    )

    for idx, sentence in enumerate(sentences):

        # -------------------------------------------------
        # 1) extracted_sentence
        # -------------------------------------------------
        sentence_row = await db.execute_returning_row(
            """
            INSERT INTO aios.extracted_sentence (
                section_id,
                sentence_index,
                sentence_text
            )
            VALUES ($1, $2, $3)
            RETURNING sentence_id
            """,
            section_id,
            idx,
            sentence,
        )
        sentence_id: UUID = sentence_row["sentence_id"]

        # -------------------------------------------------
        # 2) claim_candidate
        # -------------------------------------------------
        subject, predicate, obj = extract_spo(sentence)

        claim_row = await db.execute_returning_row(
            """
            INSERT INTO aios.claim_candidate (
                sentence_id,
                subject,
                predicate,
                object,
                raw_text,
                extraction_rule,
                extraction_ver,
                confidence,
                status
            )
            VALUES (
                $1, $2, $3, $4,
                $5,
                'spacy-dep',
                $6,
                0.0,
                'pending'
            )
            RETURNING claim_id
            """,
            sentence_id,
            subject,
            predicate,
            obj,
            sentence,
            WORKER_VERSION,
        )
        claim_id: UUID = claim_row["claim_id"]

        # -------------------------------------------------
        # 3) claim_world_assignment (schema-aligned)
        # -------------------------------------------------
        await db.execute(
            """
            INSERT INTO aios.claim_world_assignment (
                claim_id,
                world_key,
                confidence,
                assigned_by,
                assigned_at
            )
            VALUES ($1, $2, 0.5, $3, now())
            ON CONFLICT DO NOTHING
            """,
            claim_id,
            LIMINAL_WORLD_KEY,
            WORKER_NAME,
        )

        # -------------------------------------------------
        # 4) claim_provenance (document-level only)
        # -------------------------------------------------
        if document_id is not None:
            await db.execute(
                """
                INSERT INTO aios.claim_provenance (
                    claim_id,
                    document_id,
                    citation,
                    source_weight
                )
                VALUES ($1, $2, $3, 0.5)
                ON CONFLICT DO NOTHING
                """,
                claim_id,
                document_id,
                f"document_section:{section_id}",
            )


# =================================================
# CLI
# =================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main():
        db = Database(settings.db_dsn)
        await db.connect()
        try:
            await run_claim_extraction_worker(db)
        finally:
            await db.close()

    asyncio.run(main())
