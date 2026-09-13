# aios_app/pipeline/worker.py

from __future__ import annotations

import logging
from typing import List, Optional, Tuple
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.linguistic_projection import (
    parse_text,
    persist_projection,
    sentence_docs,
)
from aios_app.epistemic.pivots import resolve_subject_pivot

logger = logging.getLogger("aios.pipeline.worker")

# =================================================
# Configuration
# =================================================

LIMINAL_WORLD_KEY = "liminal"
WORKER_NAME = "claim_extractor"
WORKER_VERSION = "v6-persisted-section-fanout"


def _extract_spo_from_doc(doc) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract the shallow SPO tuple from an already-parsed sentence Doc."""
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


def _parsed_sentences_from_doc(doc) -> List[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
    parsed = []
    for sentence in sentence_docs(doc):
        subject, predicate, obj = _extract_spo_from_doc(sentence.doc)
        parsed.append((sentence.text, subject, predicate, obj))
    return parsed


def parse_section_once(
    text: str,
) -> List[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
    """Parse a section once and fan sentence-level shallow claims from it."""
    if not text:
        return []
    return _parsed_sentences_from_doc(parse_text(text))


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

    The source section is parsed exactly once with the full shared linguistic
    model. That parse is persisted as a regenerable projection and both shallow
    claim extraction and later semantic-frame decomposition fan out from it.
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

    # One expensive inference pass. Persist before fan-out so retries and
    # semantic-frame jobs can reuse the exact same linguistic analysis.
    section_doc = parse_text(content)
    await persist_projection(db, section_id=section_id, text=content, doc=section_doc)
    parsed_sentences = _parsed_sentences_from_doc(section_doc)

    parsed_by_index = {
        idx: (sentence, subject, predicate, obj)
        for idx, (sentence, subject, predicate, obj) in enumerate(parsed_sentences)
    }
    parsed_by_text = {
        sentence: (subject, predicate, obj)
        for sentence, subject, predicate, obj in parsed_sentences
    }

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
        sentences = []

        for idx, (sent, _subject, _predicate, _obj) in enumerate(parsed_sentences):
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

        parsed = parsed_by_index.get(idx)
        if parsed is not None and parsed[0] == sentence:
            _sentence, subject, predicate, obj = parsed
        else:
            # Old rows may have been created by an earlier sentence-boundary
            # model. Prefer an exact match from this projection and never spend
            # another NLP inference pass merely to recover a retry.
            spo = parsed_by_text.get(sentence)
            if spo is not None:
                subject, predicate, obj = spo
            else:
                logger.warning(
                    "Section %s sentence %s no longer matches current projection; "
                    "preserving raw claim without reparsing",
                    section_id,
                    idx,
                )
                subject = predicate = obj = None

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
        "Section %s: persisted one linguistic parse, produced %d sentences, "
        "inserted %d new claims; marked claim stage complete",
        section_id,
        len(parsed_sentences),
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
