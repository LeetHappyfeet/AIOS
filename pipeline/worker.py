# aios_app/pipeline/worker.py

from __future__ import annotations

import logging
from typing import List, Optional, Tuple
from uuid import UUID

import spacy

from aios_app.db import Database
from aios_app.epistemic.pivots import resolve_subject_pivot

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

    if root.pos_ == "VERB":
        predicate = root.lemma_

        for tok in doc:
            if subject is None and tok.dep_ in ("nsubj", "nsubjpass"):
                subject = tok.text
            if obj is None and tok.dep_ in ("dobj", "pobj", "attr"):
                obj = tok.text

        return subject, predicate, obj

    has_copula = any(child.dep_ == "cop" for child in root.children)
    if has_copula:
        for tok in doc:
            if tok.dep_ in ("nsubj", "nsubjpass"):
                subject = tok.text
                break

        complement_tokens = sorted(set(root.subtree), key=lambda t: t.i)
        obj = " ".join(tok.text for tok in complement_tokens)
        predicate = "be_definition_of"

        return subject, predicate, obj

    return None, None, None


# =================================================
# Worker entrypoint
# =================================================

async def run_claim_extraction_for_section(
    db: Database,
    *,
    section_id: UUID,
) -> None:
    """
    Section-scoped claim extraction.

    Guarantees:
      - extracted_sentence rows exist
      - claim_candidate rows exist
      - claims_extracted_at is set exactly once
      - the originating ingest_event receives claims_processed_at

    A claim_candidate is still created when shallow SPO extraction cannot
    identify all three terms. That preserves the sentence as an observational
    RDF claim and keeps the ingestion lineage complete.
    """

    row = await db.fetchrow(
        """
        SELECT
            ds.section_id,
            ds.document_id,
            ds.content,
            ds.claims_extracted_at,
            n.event_id,
            n.speaker_id,
            n.speaker_role::text AS speaker_role,
            n.recipient_id,
            n.character_id,
            NULLIF(n.payload->>'viewpoint_id', '') AS explicit_viewpoint_id,
            COALESCE(NULLIF(n.payload->>'identity_ruleset', ''), 'character-id-v1') AS identity_ruleset
        FROM aios.document_section ds
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        WHERE ds.section_id = $1
        """,
        section_id,
    )

    if not row:
        logger.warning("Section %s not found", section_id)
        return

    event_id = int(row["event_id"])

    if row["claims_extracted_at"] is not None:
        await _mark_claim_stage_complete(db, event_id)
        logger.debug("Section %s already completed; refreshed event latch", section_id)
        return

    document_id: Optional[UUID] = row["document_id"]
    content: str = row["content"]

    # -------------------------------------------------
    # 1) Load or create extracted_sentence rows
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
        sentences = [
            (r["sentence_id"], int(r["sentence_index"]), r["sentence_text"])
            for r in existing
        ]
    else:
        text_sentences = split_sentences(content)
        sentences = []

        for idx, sent in enumerate(text_sentences):
            r = await db.execute_returning_row(
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
            sentences.append((r["sentence_id"], idx, sent))

    # -------------------------------------------------
    # 2) Insert missing claim_candidate rows
    # -------------------------------------------------

    inserted = 0

    for sentence_id, idx, sentence in sentences:
        exists = await db.fetchrow(
            """
            SELECT 1
            FROM aios.claim_candidate
            WHERE sentence_id = $1
            """,
            sentence_id,
        )
        if exists:
            continue

        subject, predicate, obj = extract_spo(sentence)
        pivot = resolve_subject_pivot(
            subject,
            character_id=row["character_id"],
            speaker_id=row["speaker_id"],
            speaker_role=row["speaker_role"],
            recipient_id=row["recipient_id"],
            viewpoint_id=row["explicit_viewpoint_id"],
            ruleset_id=row["identity_ruleset"],
        )
        subject = pivot.subject

        r = await db.execute_returning_row(
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
                $7,
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
            "spacy-dep+character-pivot-v1" if pivot.resolved else "spacy-dep",
        )

        claim_id = r["claim_id"]
        inserted += 1

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

        if document_id:
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

    # -------------------------------------------------
    # 3) Mark section and originating event complete for this stage
    # -------------------------------------------------

    await db.execute(
        """
        UPDATE aios.document_section
        SET claims_extracted_at = now()
        WHERE section_id = $1
          AND claims_extracted_at IS NULL
        """,
        section_id,
    )

    await _mark_claim_stage_complete(db, event_id)

    logger.info(
        "Section %s: inserted %d new claims; marked claim stage complete",
        section_id,
        inserted,
    )


async def _mark_claim_stage_complete(db: Database, event_id: int) -> None:
    await db.execute(
        """
        UPDATE aios.ingest_event
        SET claims_processed_at = COALESCE(claims_processed_at, now()),
            process_status = CASE
                WHEN rdf_processed_at IS NOT NULL THEN 'done'::aios.process_status
                ELSE 'processing'::aios.process_status
            END,
            process_error = NULL
        WHERE event_id = $1
        """,
        event_id,
    )
