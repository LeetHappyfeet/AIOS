from __future__ import annotations

"""Semantic-aware frame decomposition facade.

The v2 linguistic decomposer remains the English adapter. After it stores its
resolved frames, this facade interprets every frame into the language-neutral
semantic vocabulary and persists typed semantic roles/content links.

Semantic decomposition reuses the persisted source-level linguistic projection
when possible. A conservative scope/boundary adapter now sits in front of the
legacy English decomposer so imagined/conditional content cannot silently
become asserted and strong punctuation cannot turn discourse markers into
subjects.
"""

from contextvars import ContextVar
from dataclasses import replace
import json
import logging
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic import semantic_frames_legacy as legacy
from aios_app.epistemic.epistemic_scope import (
    SCOPE_ASSERTED,
    classify_scope,
    effective_scope,
    split_strong_clauses,
)
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


_LEGACY_DECOMPOSE_SENTENCE = legacy.decompose_sentence


def _scoped_decompose_sentence(sentence: str):
    """Decompose NLP sentences and strong clauses as independent frame forests."""
    clean = (sentence or "").strip()
    if not clean:
        return []

    # claim_candidate.raw_text is not guaranteed to contain exactly one
    # grammatical sentence.  Parse the full text once, then prevent frame
    # topology from crossing sentence boundaries before applying the existing
    # strong-punctuation splitter inside each sentence.
    doc = legacy._get_nlp()(clean)
    sentence_parts = [span.text.strip() for span in doc.sents if span.text.strip()]
    if not sentence_parts:
        sentence_parts = [clean]

    combined = []
    offset = 0
    segmented = len(sentence_parts) > 1
    for sentence_part in sentence_parts:
        sentence_scope = classify_scope(sentence_part)
        parts = split_strong_clauses(sentence_part) or [sentence_part]
        segmented = segmented or len(parts) > 1
        for part in parts:
            drafts = _LEGACY_DECOMPOSE_SENTENCE(part)
            part_scope = effective_scope(part, sentence_scope)
            for draft in drafts:
                meta = dict(draft.meta)
                meta.update({
                    "epistemic_scope_policy": "epistemic-scope-v1",
                    "local_modality": draft.modality,
                    "effective_modality": part_scope if part_scope != SCOPE_ASSERTED else draft.modality,
                    "strong_clause_segmented": segmented,
                    "sentence_local_decomposition": True,
                })
                modality = part_scope if part_scope != SCOPE_ASSERTED else draft.modality
                combined.append(replace(
                    draft,
                    index=draft.index + offset,
                    parent_index=(draft.parent_index + offset if draft.parent_index is not None else None),
                    object_frame_index=(
                        draft.object_frame_index + offset
                        if draft.object_frame_index is not None
                        else None
                    ),
                    modality=modality,
                    discourse_mode=(
                        part_scope if part_scope != SCOPE_ASSERTED else draft.discourse_mode
                    ),
                    meta=meta,
                ))
            offset += len(drafts)
    return combined


# The legacy DB writer resolves this global at call time. Installing the wrapper
# here lets all normal semantic-frame jobs use the corrected decomposition while
# preserving the established storage contract and resolver implementation.
if legacy.decompose_sentence is not _scoped_decompose_sentence:
    legacy.decompose_sentence = _scoped_decompose_sentence


# Compatibility export used by existing unit tests/debugging.
def decompose_sentence(sentence: str):
    _install_projection_aware_nlp()
    return _scoped_decompose_sentence(sentence)


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
