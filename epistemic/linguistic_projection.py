from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import spacy
from spacy.tokens import Doc

from aios_app.db import Database

logger = logging.getLogger("aios.epistemic.linguistic_projection")

PARSER_NAME = "spacy"
PARSER_VERSION = "en_core_web_sm"
PROJECTION_VERSION = "linguistic-projection-v1"

_NLP = None


@dataclass(frozen=True)
class ParsedSentence:
    index: int
    text: str
    doc: Doc


def get_nlp():
    """Load the full linguistic model once per process.

    NER stays enabled because semantic-frame decomposition consumes named
    entities. textcat is unused by AIOS and remains disabled.
    """
    global _NLP
    if _NLP is None:
        logger.info("Loading spaCy %s for shared linguistic projection", PARSER_VERSION)
        _NLP = spacy.load(PARSER_VERSION, disable=["textcat"])
        logger.info("Shared linguistic projection model ready")
    return _NLP


def content_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def parse_text(text: str) -> Doc:
    return get_nlp()(text or "")


def serialize_doc(doc: Doc) -> dict:
    return doc.to_json()


def deserialize_doc(payload: dict) -> Doc:
    doc = Doc(get_nlp().vocab)
    doc.from_json(payload)
    return doc


def sentence_docs(doc: Doc) -> list[ParsedSentence]:
    """Return sentence-local Docs without rerunning the NLP pipeline.

    Span.as_doc() copies the parser/NER/morphology annotations from the source
    document while rebasing token and character offsets to the sentence. This
    makes the result compatible with the existing sentence-level semantic
    decomposer without a second spaCy inference pass.
    """
    out: list[ParsedSentence] = []
    for index, span in enumerate(doc.sents):
        text = span.text.strip()
        if not text:
            continue
        local_doc = span.as_doc()
        # Leading/trailing whitespace is normally excluded by sentence spans,
        # but guard against a future tokenizer change that makes text differ.
        if local_doc.text.strip() != text:
            logger.debug(
                "Sentence %s projection text differs only by boundary whitespace",
                index,
            )
        out.append(ParsedSentence(index=index, text=text, doc=local_doc))
    return out


async def persist_projection(
    db: Database,
    *,
    section_id: UUID,
    text: str,
    doc: Optional[Doc] = None,
) -> Doc:
    """Persist a regenerable source-level linguistic projection."""
    doc = doc if doc is not None else parse_text(text)
    payload = serialize_doc(doc)
    await db.execute(
        """
        INSERT INTO aios.linguistic_projection (
            section_id, parser_name, parser_version, projection_version,
            content_sha256, doc_json, created_at, updated_at
        )
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,now(),now())
        ON CONFLICT (section_id) DO UPDATE
        SET parser_name=EXCLUDED.parser_name,
            parser_version=EXCLUDED.parser_version,
            projection_version=EXCLUDED.projection_version,
            content_sha256=EXCLUDED.content_sha256,
            doc_json=EXCLUDED.doc_json,
            updated_at=now()
        """,
        section_id,
        PARSER_NAME,
        PARSER_VERSION,
        PROJECTION_VERSION,
        content_sha256(text),
        json.dumps(payload),
    )
    return doc


async def load_projection(
    db: Database,
    *,
    section_id: UUID,
    expected_text: Optional[str] = None,
) -> Optional[Doc]:
    row = await db.fetchrow(
        """
        SELECT parser_name, parser_version, projection_version,
               content_sha256, doc_json
        FROM aios.linguistic_projection
        WHERE section_id=$1
        """,
        section_id,
    )
    if not row:
        return None
    if (
        row["parser_name"] != PARSER_NAME
        or row["parser_version"] != PARSER_VERSION
        or row["projection_version"] != PROJECTION_VERSION
    ):
        return None
    if expected_text is not None and row["content_sha256"] != content_sha256(expected_text):
        return None
    payload = row["doc_json"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return deserialize_doc(payload)


async def ensure_projection(
    db: Database,
    *,
    section_id: UUID,
    text: str,
) -> Doc:
    cached = await load_projection(db, section_id=section_id, expected_text=text)
    if cached is not None:
        return cached
    doc = parse_text(text)
    return await persist_projection(db, section_id=section_id, text=text, doc=doc)


def sentence_doc_by_index(doc: Doc, sentence_index: int, expected_text: Optional[str] = None) -> Optional[Doc]:
    sentences = sentence_docs(doc)
    if 0 <= sentence_index < len(sentences):
        candidate = sentences[sentence_index]
        if expected_text is None or candidate.text == expected_text:
            return candidate.doc
    if expected_text is not None:
        for candidate in sentences:
            if candidate.text == expected_text:
                return candidate.doc
    return None
