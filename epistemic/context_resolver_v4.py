from __future__ import annotations

"""Semantic-first context resolver.

The legacy resolver remains responsible for lineage, ownership, viewpoint,
acquisition mode, runtime binding and initial RDF plumbing. This wrapper adds
a language-neutral semantic interpretation step and rewrites semantic context
consistently in SQL and RDF before normalization consumes it.
"""

import json
from urllib.parse import quote
from uuid import UUID

from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient
from aios_app.epistemic import context_resolver_legacy as legacy
from aios_app.epistemic.semantic_interpreter import (
    SEMANTIC_INTERPRETER_VERSION,
    interpret_frame,
)

RESOLVER_VERSION = "context-resolver-v4-semantic"


def _sparql_lit(value: str | None) -> str:
    if value is None:
        return '""'
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


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


async def _write_semantic_rdf(fuseki: FusekiClient, context) -> None:
    claim_iri = f"urn:aios:world:claim:{context.claim_id}"
    triples = [
        f"<{claim_iri}> world:claimKind world:{context.claim_kind.title()} .",
        f"<{claim_iri}> world:predicateFamily world:{context.predicate_family.title()} .",
        f"<{claim_iri}> world:epistemicScope {_sparql_lit(context.epistemic_scope)} .",
        f"<{claim_iri}> world:contextResolverVersion {_sparql_lit(RESOLVER_VERSION)} .",
        f"<{claim_iri}> world:subjectIsPivot {_sparql_lit(str(context.subject_is_pivot).lower())} .",
        f"<{claim_iri}> world:objectIsPivot {_sparql_lit(str(context.object_is_pivot).lower())} .",
    ]
    if context.subject_kind:
        triples.append(f"<{claim_iri}> world:subjectKind world:{context.subject_kind.title()} .")
    if context.object_kind:
        triples.append(f"<{claim_iri}> world:objectKind world:{context.object_kind.title()} .")
    if context.origin_character_id:
        segment = quote(context.origin_character_id, safe="")
        triples.append(f"<{claim_iri}> world:originCharacter <urn:aios:character:{segment}> .")
    if context.source_id:
        triples.append(f"<{claim_iri}> world:sourceId {_sparql_lit(context.source_id)} .")
    if context.source_kind:
        triples.append(f"<{claim_iri}> world:sourceKind {_sparql_lit(context.source_kind)} .")
    if context.target_character_id:
        triples.append(f"<{claim_iri}> world:targetCharacterHint {_sparql_lit(context.target_character_id)} .")
    if context.target_world_id:
        triples.append(f"<{claim_iri}> world:targetWorldHint <urn:aios:world:{context.target_world_id}> .")
    if context.viewpoint_id:
        triples.append(f"<{claim_iri}> world:viewpointId {_sparql_lit(context.viewpoint_id)} .")
    if context.character_instance_id:
        triples.append(f"<{claim_iri}> world:originCharacterInstance <urn:aios:character-instance:{context.character_instance_id}> .")
    if context.world_id:
        triples.append(f"<{claim_iri}> world:originWorld <urn:aios:world:{context.world_id}> .")
    if context.timeline_id:
        triples.append(f"<{claim_iri}> world:originTimeline <urn:aios:timeline:{context.timeline_id}> .")
    if context.acquisition_mode:
        triples.append(f"<{claim_iri}> world:acquisitionMode {_sparql_lit(context.acquisition_mode)} .")

    clear = f"""
PREFIX world: <urn:aios:world#>
DELETE {{ GRAPH <{legacy.LIMINAL_GRAPH}> {{ <{claim_iri}> ?p ?o . }} }}
WHERE {{
  GRAPH <{legacy.LIMINAL_GRAPH}> {{
    <{claim_iri}> ?p ?o .
    FILTER (?p IN (
      world:claimKind, world:predicateFamily, world:epistemicScope,
      world:contextResolverVersion, world:subjectIsPivot, world:objectIsPivot,
      world:subjectKind, world:objectKind, world:acquisitionMode
    ))
  }}
}}
""".strip()
    fuseki.update(legacy.DATASET, clear)
    insert = f"""
PREFIX world: <urn:aios:world#>
INSERT DATA {{
  GRAPH <{legacy.LIMINAL_GRAPH}> {{
    {chr(10).join(triples)}
  }}
}}
""".strip()
    fuseki.update(legacy.DATASET, insert)


async def resolve_claim_context(
    db: Database,
    fuseki: FusekiClient,
    *,
    claim_id: UUID,
):
    context = await legacy.resolve_claim_context(db, fuseki, claim_id=claim_id)
    frame = await _primary_frame(db, claim_id)
    if not frame:
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

    semantic_meta = interpretation.as_meta()
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

    await _write_semantic_rdf(fuseki, corrected)
    await legacy._log_rdf_context(db, corrected)
    await db.execute(
        """
        UPDATE aios.rdf_promotion_log
        SET rdf_object=$2,
            promoted_by='context_resolver_v4_semantic',
            promotion_meta=COALESCE(promotion_meta,'{}'::jsonb) || $3::jsonb,
            promoted_at=now()
        WHERE claim_id=$1
          AND rdf_dataset=$4
          AND rdf_graph=$5
          AND rdf_predicate=$6
        """,
        claim_id,
        RESOLVER_VERSION,
        json.dumps({"semantic_interpreter_version": SEMANTIC_INTERPRETER_VERSION}),
        legacy.DATASET,
        legacy.LIMINAL_GRAPH,
        legacy.RDF_RECEIPT_PREDICATE,
    )
    return corrected
