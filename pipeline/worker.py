# aios_app/pipeline/worker.py

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
        for tok in doc:
            if tok.dep_ in ("nsubj", "nsubjpass"):
                subject = tok.text
                break

        complement_tokens = [root] + list(root.subtree)
        complement_tokens = sorted(set(complement_tokens), key=lambda t: t.i)
        obj = " ".join(tok.text for tok in complement_tokens)

        predicate = "be_definition_of"
        return subject, predicate, obj

    return None, None, None


# =================================================
# Worker
# =================================================

async def run_claim_extraction_worker(db: Database) -> None:
    """
    document_section → (ensure extracted_sentence) → claim_candidate (+ provenance/assignment)

    IMPORTANT:
    - This worker no longer gates on extracted_sentence being absent.
    - It gates on missing claim_candidate rows.
    """

    logger.info("Starting claim extraction worker (claim-gated)")

    # -------------------------------------------------
    # Find sections that still have work:
    #   at least one extracted_sentence WITHOUT a claim_candidate
    #   OR no extracted_sentences at all yet
    # -------------------------------------------------
    rows = await db.fetch(
        """
        SELECT
            ds.section_id,
            ds.document_id,
            ds.node_id,
            ds.section_order,
            ds.content,
            COUNT(es.sentence_id) AS sentence_count,
            COUNT(cc.claim_id)    AS claim_count
        FROM aios.document_section ds
        LEFT JOIN aios.extracted_sentence es
          ON es.section_id = ds.section_id
        LEFT JOIN aios.claim_candidate cc
          ON cc.sentence_id = es.sentence_id
        GROUP BY
            ds.section_id, ds.document_id, ds.node_id, ds.section_order, ds.content
        HAVING
            COUNT(es.sentence_id) = 0
            OR COUNT(cc.claim_id) < COUNT(es.sentence_id)
        ORDER BY ds.section_order
        """
    )

    logger.info("Found %d sections needing claim extraction", len(rows))

    for row in rows:
        section_id: UUID = row["section_id"]
        try:
            await _process_section(
                db=db,
                section_id=section_id,
                document_id=row["document_id"],
                node_id=row["node_id"],
                content=row["content"],
            )
        except Exception:
            logger.exception("Failed processing section %s", section_id)

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
    Ensure extracted_sentence exists for this section, then create missing claim_candidate
    rows for those sentences only.
    """

    # -------------------------------------------------
    # 0) Load existing extracted sentences (if any)
    # -------------------------------------------------
    existing = await db.fetch(
        """
        SELECT sentence_id, sentence_index, sentence_text
        FROM aios.extracted_sentence
        WHERE section_id = $1
        ORDER BY sentence_index
        """,
        section_id,
    )

    if existing:
        sentences = [(r["sentence_id"], int(r["sentence_index"]), r["sentence_text"]) for r in existing]
        logger.debug(
            "Section %s already has %d extracted_sentence rows",
            section_id,
            len(sentences),
        )
    else:
        # -------------------------------------------------
        # 0b) If none exist, create them from content
        # -------------------------------------------------
        text_sentences = split_sentences(content)
        logger.debug(
            "Section %s had no extracted sentences; splitting content into %d sentences",
            section_id,
            len(text_sentences),
        )

        sentences = []
        for idx, sent in enumerate(text_sentences):
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
                sent,
            )
            sentences.append((sentence_row["sentence_id"], idx, sent))

    # -------------------------------------------------
    # 1) For each sentence, create claim only if missing
    # -------------------------------------------------
    inserted_claims = 0

    for sentence_id, idx, sentence in sentences:
        # Does a claim already exist for this sentence?
        exists = await db.fetchrow(
            """
            SELECT 1
            FROM aios.claim_candidate
            WHERE sentence_id = $1
            LIMIT 1
            """,
            sentence_id,
        )
        if exists:
            continue

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
        inserted_claims += 1

        # claim_world_assignment
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

        # claim_provenance
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
                f"document_section:{section_id}:sentence_index:{idx}",
            )

    logger.info(
        "Section %s: inserted %d new claims",
        section_id,
        inserted_claims,
    )


# =================================================
# CLI
# =================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main() -> None:
        db = Database(settings.db_dsn)
        await db.connect()
        try:
            await run_claim_extraction_worker(db)
        finally:
            await db.close()

    asyncio.run(main())
