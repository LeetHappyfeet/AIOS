from __future__ import annotations

"""Semantic-aware frame decomposition facade.

The v2 linguistic decomposer remains the English adapter.  After it stores its
resolved frames, this facade interprets every frame into the language-neutral
semantic vocabulary and marks whether the frame is eligible to stand alone as
an atomic proposition.
"""

import json
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic import semantic_frames_legacy as legacy
from aios_app.epistemic.semantic_interpreter import interpret_frame

DECOMPOSER_VERSION = legacy.DECOMPOSER_VERSION
REFERENT_RESOLVER_VERSION = legacy.REFERENT_RESOLVER_VERSION
PERSPECTIVE_VERSION = legacy.PERSPECTIVE_VERSION
FrameDraft = legacy.FrameDraft

# Compatibility exports used by tests/debugging.
decompose_sentence = legacy.decompose_sentence


async def _interpret_claim_frames(db: Database, claim_id: UUID) -> None:
    frames = await db.fetch(
        """
        SELECT
            frame_id,
            COALESCE(resolved_subject, subject_text) AS subject,
            COALESCE(resolved_object, object_text) AS object_value,
            predicate_surface,
            predicate_canonical,
            frame_role,
            resolution_status,
            object_frame_id,
            meta
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


async def decompose_claim_frames(db: Database, *, claim_id: UUID) -> int:
    count = await legacy.decompose_claim_frames(db, claim_id=claim_id)
    # Even when the legacy projection is already current, semantic metadata may
    # have been introduced after the frame rows were originally decomposed.
    await _interpret_claim_frames(db, claim_id)
    return count
