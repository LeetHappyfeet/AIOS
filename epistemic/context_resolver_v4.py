from __future__ import annotations

"""Semantic-first SQL context resolver.

Generation-critical cognition must not synchronously project RDF. The legacy
resolver is still used for lineage/ownership/viewpoint/runtime binding, but its
Fuseki writes are suppressed here. RDF projection is an enrichment concern and
runs later through the RDF pipeline.
"""

import json
from uuid import UUID

from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient
from aios_app.epistemic import context_resolver_legacy as legacy
from aios_app.epistemic.semantic_interpreter import (
    SEMANTIC_INTERPRETER_VERSION,
    interpret_frame,
)

RESOLVER_VERSION = "context-resolver-v4-semantic"


class _DeferredFuseki:
    """Compatibility sink used while the legacy resolver is being retired.

    The legacy resolver still constructs SPARQL, but no network I/O occurs on
    the semantic worker. We immediately remove its RDF receipt so downstream
    code cannot mistake a deferred projection for a completed one.
    """

    def update(self, dataset: str, sparql: str) -> None:
        return None


async def _primary_frame(db: Database, claim_id: UUID):
    return await db.fetchrow(
        """
        SELECT
            f.frame_id,
            COALESCE(f.resolved_subject, f.subject_text) AS subject,
            COALESCE(f.resolved_object, f.object_text) AS object_value,
            f.predicate_surface,
            f.predicate_canonical,
            f.frame_role,
            f.resolution_status,
            f.object_frame_id,
            f.frame_confidence,
            f.meta
        FROM aios.claim_semantic_frame_projection p
        JOIN aios.claim_semantic_frame f ON f.frame_id=p.primary_frame_id
        WHERE p.claim_id=$1
        """,
        claim_id,
    )


async def _clear_deferred_rdf_receipt(db: Database, claim_id: UUID) -> None:
    await db.execute(
        """
        DELETE FROM aios.rdf_promotion_log
        WHERE claim_id=$1
          AND rdf_dataset=$2
          AND rdf_graph=$3
          AND rdf_predicate=$4
        """,
        claim_id,
        legacy.DATASET,
        legacy.LIMINAL_GRAPH,
        legacy.RDF_RECEIPT_PREDICATE,
    )


async def resolve_claim_context(
    db: Database,
    fuseki: FusekiClient | None = None,
    *,
    claim_id: UUID,
):
    # Keep the legacy SQL resolver temporarily, but make its RDF side effect a
    # no-op. The public fuseki parameter remains during the compatibility window
    # so existing handlers do not need a flag-day interface change.
    context = await legacy.resolve_claim_context(
        db,
        _DeferredFuseki(),
        claim_id=claim_id,
    )
    await _clear_deferred_rdf_receipt(db, claim_id)

    frame = await _primary_frame(db, claim_id)
    if not frame:
        # Upgrade the resolver receipt in SQL even for claims without a semantic
        # frame correction. This removes the old v3 completion contract.
        await db.execute(
            """
            UPDATE aios.claim_context_resolution
            SET resolver_version=$2,
                meta=COALESCE(meta,'{}'::jsonb) || $3::jsonb,
                resolved_at=now()
            WHERE claim_id=$1
            """,
            claim_id,
            RESOLVER_VERSION,
            json.dumps({"rdf_projection": "deferred"}),
        )
        return context

    meta = frame["meta"] if isinstance(frame["meta"], dict) else {}
    interpretation = interpret_frame(
        predicate=frame["predicate_canonical"],
        predicate_surface=frame["predicate_surface"],
        subject=frame["subject"],
        object_value=frame["object_value"],
        frame_role=frame["frame_role"],
        resolution_status=frame["resolution_status"],
        object_frame_id=frame["object_frame_id"],
        meta=meta,
    )

    family = interpretation.predicate_family
    claim_kind = interpretation.claim_kind
    if family == "UNKNOWN":
        family = context.predicate_family
    if claim_kind == "UNKNOWN":
        claim_kind = context.claim_kind

    new_predicate = frame["predicate_canonical"]
    if frame["predicate_surface"] == "be" and interpretation.semantic_type != "IDENTITY":
        new_predicate = "be"

    semantic_meta = {
        **interpretation.as_meta(),
        "rdf_projection": "deferred",
    }
    await db.execute(
        """
        UPDATE aios.claim_semantic_frame
        SET predicate_canonical=$2,
            meta=COALESCE(meta,'{}'::jsonb) || $3::jsonb
        WHERE frame_id=$1
        """,
        frame["frame_id"],
        new_predicate,
        json.dumps(semantic_meta),
    )

    await db.execute(
        """
        UPDATE aios.claim_context_resolution
        SET claim_kind=$2,
            predicate_family=$3,
            resolver_version=$4,
            confidence=GREATEST(confidence, $5),
            meta=COALESCE(meta,'{}'::jsonb) || $6::jsonb,
            resolved_at=now()
        WHERE claim_id=$1
        """,
        claim_id,
        claim_kind,
        family,
        RESOLVER_VERSION,
        float(interpretation.confidence),
        json.dumps(semantic_meta),
    )

    corrected = legacy.ClaimContext(
        claim_id=context.claim_id,
        claim_kind=claim_kind,
        subject_kind=context.subject_kind,
        object_kind=context.object_kind,
        predicate_family=family,
        origin_character_id=context.origin_character_id,
        character_instance_id=context.character_instance_id,
        speaker_id=context.speaker_id,
        speaker_type=context.speaker_type,
        viewpoint_id=context.viewpoint_id,
        source_id=context.source_id,
        source_kind=context.source_kind,
        target_character_id=context.target_character_id,
        target_world_id=context.target_world_id,
        world_id=context.world_id,
        timeline_id=context.timeline_id,
        dag_node_id=context.dag_node_id,
        epistemic_scope=context.epistemic_scope,
        acquisition_mode=context.acquisition_mode,
        subject_is_pivot=context.subject_is_pivot,
        object_is_pivot=context.object_is_pivot,
        confidence=max(context.confidence, float(interpretation.confidence)),
    )

    await _clear_deferred_rdf_receipt(db, claim_id)
    return corrected
