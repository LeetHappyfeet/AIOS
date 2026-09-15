from __future__ import annotations

"""Semantic-aware frame decomposition facade.

The v2 linguistic decomposer remains the English adapter. After it stores its
resolved frames, this facade interprets every frame into the language-neutral
semantic vocabulary and persists typed semantic roles/content links.

Semantic decomposition now reuses the persisted source-level linguistic
projection when possible. The legacy decomposer itself remains unchanged: a
projection-aware NLP adapter supplies an already-parsed sentence Doc, so the
semantic behavior is preserved while repeated spaCy inference is avoided.
"""

from contextvars import ContextVar
import json
import logging
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic import semantic_frames_legacy as legacy
from aios_app.epistemic.linguistic_projection import (
    ensure_projection,
    get_nlp,
    sentence_doc_by_index,
)
from aios_app.epistemic.semantic_interpreter import interpret_frame

logger = logging.getLogger("aios.epistemic.semantic_frames")

DECOMPOSER_VERSION = legacy.DECOMPOSER_VERSION
REFERENT_RESOLVER_VERSION = legacy.REFERENT_RESOLVER_VERSION
PERSPECTIVE_VERSION = legacy.PERSPECTIVE_VERSION
FrameDraft = legacy.FrameDraft

_PROJECTED_DOCS: ContextVar[dict[str, object]] = ContextVar(
    "aios_projected_semantic_docs",
    default={},
)


class _ProjectionAwareNLP:
    """Drop-in spaCy Language proxy with task-local parsed-Doc reuse."""

    def __init__(self, base_nlp):
        self._base_nlp = base_nlp

    def __call__(self, text, *args, **kwargs):
        projected = _PROJECTED_DOCS.get().get(text)
        if projected is not None:
            return projected
        return self._base_nlp(text, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base_nlp, name)


def _install_projection_aware_nlp() -> None:
    current = legacy._NLP
    if isinstance(current, _ProjectionAwareNLP):
        return
    base = current if current is not None else get_nlp()
    legacy._NLP = _ProjectionAwareNLP(base)


# Compatibility export used by existing unit tests/debugging. Outside a
# projected context this behaves exactly like the old sentence parser.
def decompose_sentence(sentence: str):
    _install_projection_aware_nlp()
    return legacy.decompose_sentence(sentence)


_SUBJECT_ROLE = {
    "DESIRE": "experiencer",
    "INTENTION": "agent",
    "MENTAL_STATE": "experiencer",
    "MEMORY": "rememberer",
    "COMMUNICATION": "speaker",
    "ACTION": "agent",
    "CAUSE": "cause",
    "POSSESSION": "possessor",
    "LOCATION": "located_entity",
    "RELATION": "source",
    "DESCRIPTION": "entity",
    "IDENTITY": "entity",
    "RULE": "subject",
    "TEMPORAL": "event",
    "UNKNOWN": "subject",
}
_OBJECT_ROLE = {
    "DESIRE": "content",
    "INTENTION": "goal",
    "MENTAL_STATE": "content",
    "MEMORY": "content",
    "COMMUNICATION": "content",
    "ACTION": "patient",
    "CAUSE": "effect",
    "POSSESSION": "possessed",
    "LOCATION": "location",
    "RELATION": "target",
    "DESCRIPTION": "attribute",
    "IDENTITY": "class",
    "RULE": "constraint",
    "TEMPORAL": "time",
    "UNKNOWN": "object",
}


async def _projected_sentence_for_claim(db: Database, claim_id: UUID):
    row = await db.fetchrow(
        """
        SELECT
            es.section_id,
            es.sentence_index,
            es.sentence_text,
            ds.content AS section_text
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
        JOIN aios.document_section ds ON ds.section_id=es.section_id
        WHERE cc.claim_id=$1
        """,
        claim_id,
    )
    if not row:
        return None

    try:
        section_doc = await ensure_projection(
            db,
            section_id=row["section_id"],
            text=row["section_text"],
        )
        return sentence_doc_by_index(
            section_doc,
            int(row["sentence_index"]),
            expected_text=row["sentence_text"],
        )
    except Exception:
        # Projection reuse is an optimization, never a correctness dependency.
        logger.exception(
            "Failed to load linguistic projection for claim %s; falling back to sentence parse",
            claim_id,
        )
        return None


async def _persist_interpretation(db: Database, frame, semantic) -> None:
    interpretation = await db.execute_returning_row(
        """
        INSERT INTO aios.semantic_interpretation (
            claim_id, frame_id, semantic_type, source_language,
            standalone_semantic, confidence, interpreter_version, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
        ON CONFLICT (frame_id) DO UPDATE
        SET semantic_type=EXCLUDED.semantic_type,
            source_language=EXCLUDED.source_language,
            standalone_semantic=EXCLUDED.standalone_semantic,
            confidence=EXCLUDED.confidence,
            interpreter_version=EXCLUDED.interpreter_version,
            meta=EXCLUDED.meta,
            interpreted_at=now()
        RETURNING interpretation_id
        """,
        frame["claim_id"],
        frame["frame_id"],
        semantic.semantic_type,
        semantic.source_language,
        semantic.standalone_semantic,
        float(semantic.confidence),
        semantic.interpreter_version,
        json.dumps({"semantic_cues": list(semantic.cues)}),
    )
    interpretation_id = interpretation["interpretation_id"]
    await db.execute(
        "DELETE FROM aios.semantic_interpretation_role WHERE interpretation_id=$1",
        interpretation_id,
    )

    subject = frame["subject"]
    if subject:
        await db.execute(
            """
            INSERT INTO aios.semantic_interpretation_role (
                interpretation_id, role_name, ordinal, value_text,
                entity_key, confidence, meta
            )
            VALUES ($1,$2,0,$3,$4,$5,'{}'::jsonb)
            """,
            interpretation_id,
            _SUBJECT_ROLE.get(semantic.semantic_type, "subject"),
            subject,
            frame["subject_entity_key"],
            float(frame["referent_confidence"] or semantic.confidence),
        )

    if frame["object_frame_id"] is not None:
        await db.execute(
            """
            INSERT INTO aios.semantic_interpretation_role (
                interpretation_id, role_name, ordinal, child_frame_id,
                confidence, meta
            )
            VALUES ($1,'content',0,$2,$3,$4::jsonb)
            """,
            interpretation_id,
            frame["object_frame_id"],
            float(semantic.confidence),
            json.dumps({"nested_semantic_content": True}),
        )
    elif frame["object_value"]:
        await db.execute(
            """
            INSERT INTO aios.semantic_interpretation_role (
                interpretation_id, role_name, ordinal, value_text,
                entity_key, confidence, meta
            )
            VALUES ($1,$2,0,$3,$4,$5,'{}'::jsonb)
            """,
            interpretation_id,
            _OBJECT_ROLE.get(semantic.semantic_type, "object"),
            frame["object_value"],
            frame["object_entity_key"],
            float(frame["referent_confidence"] or semantic.confidence),
        )


async def _interpret_claim_frames(db: Database, claim_id: UUID) -> None:
    frames = await db.fetch(
        """
        SELECT
            claim_id, frame_id,
            COALESCE(resolved_subject, subject_text) AS subject,
            COALESCE(resolved_object, object_text) AS object_value,
            subject_entity_key, object_entity_key,
            predicate_surface, predicate_canonical,
            frame_role, resolution_status, object_frame_id,
            referent_confidence, meta
        FROM aios.claim_semantic_frame
        WHERE claim_id=$1
          AND decomposer_version=$2
        ORDER BY frame_index
        """,
        claim_id,
        DECOMPOSER_VERSION,
    )
    for frame in frames:
        frame_meta = frame["meta"] if isinstance(frame["meta"], dict) else {}
        semantic = interpret_frame(
            predicate=frame["predicate_canonical"],
            predicate_surface=frame["predicate_surface"],
            subject=frame["subject"],
            object_value=frame["object_value"],
            frame_role=frame["frame_role"],
            resolution_status=frame["resolution_status"],
            object_frame_id=frame["object_frame_id"],
            meta=frame_meta,
        )
        predicate = frame["predicate_canonical"]
        if frame["predicate_surface"] == "be" and semantic.semantic_type != "IDENTITY":
            predicate = "be"
        await db.execute(
            """
            UPDATE aios.claim_semantic_frame
            SET predicate_canonical=$2,
                meta=COALESCE(meta,'{}'::jsonb) || $3::jsonb
            WHERE frame_id=$1
            """,
            frame["frame_id"],
            predicate,
            json.dumps(semantic.as_meta()),
        )
        await _persist_interpretation(db, frame, semantic)


async def decompose_claim_frames(db: Database, *, claim_id: UUID) -> int:
    _install_projection_aware_nlp()
    sentence_doc = await _projected_sentence_for_claim(db, claim_id)

    token = None
    if sentence_doc is not None:
        row = await db.fetchrow(
            "SELECT raw_text FROM aios.claim_candidate WHERE claim_id=$1",
            claim_id,
        )
        if row and row["raw_text"] and sentence_doc.text.strip() == row["raw_text"].strip():
            token = _PROJECTED_DOCS.set({row["raw_text"]: sentence_doc})

    try:
        count = await legacy.decompose_claim_frames(db, claim_id=claim_id)
    finally:
        if token is not None:
            _PROJECTED_DOCS.reset(token)

    # Re-interpret even if linguistic v2 is already current: semantic versions
    # evolve independently from source-language decomposition.
    await _interpret_claim_frames(db, claim_id)
    return count
