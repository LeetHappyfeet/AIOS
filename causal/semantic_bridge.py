from __future__ import annotations

from typing import Optional
from uuid import UUID

from aios_app.db import Database

from .kernel import CausalIntegrityKernel
from .types import CausalCandidate, CausalEvaluation


_LOCATION_PREDICATES = {
    "located_at",
    "location",
    "be_in",
    "be_at",
    "inside",
    "enter",
    "arrive",
    "depart",
    "leave",
}
_EXPLICIT_MOVEMENT = {"enter", "arrive", "depart", "leave"}


async def compile_location_candidate(
    db: Database,
    *,
    claim_id: UUID,
    world_id: UUID,
    timeline_id: UUID,
    allow_latent_transition: bool = False,
) -> Optional[CausalCandidate]:
    """Compile a resolved semantic location frame into proposed reality.

    This function never commits.  Narrative, speech, memory and belief remain
    epistemic evidence until some explicit caller asks the causal kernel to
    evaluate/admit the returned candidate into a concrete /world branch.
    """

    row = await db.fetchrow(
        """
        SELECT
            sf.frame_id,
            COALESCE(sf.predicate_canonical, cc.predicate) AS predicate,
            COALESCE(sf.subject_entity_key, sf.resolved_subject, sf.subject_text) AS subject_key,
            COALESCE(sf.object_entity_key, sf.resolved_object, sf.object_text) AS object_key,
            ccr.predicate_family,
            ccr.epistemic_scope,
            ccr.dag_node_id,
            dn.event_time,
            cc.claim_id
        FROM aios.claim_candidate cc
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
        JOIN aios.claim_semantic_frame_projection sfp ON sfp.claim_id=cc.claim_id
        JOIN aios.claim_semantic_frame sf ON sf.frame_id=sfp.primary_frame_id
        LEFT JOIN aios.dag_node dn ON dn.node_id=ccr.dag_node_id
        WHERE cc.claim_id=$1
        """,
        claim_id,
    )
    if not row or row["predicate_family"] != "SPATIAL":
        return None

    predicate = str(row["predicate"] or "").strip().lower().replace(" ", "_")
    if predicate not in _LOCATION_PREDICATES:
        return None

    subject = await _resolve_unique_world_entity(
        db,
        world_id=world_id,
        key=str(row["subject_key"] or ""),
        require_location=False,
    )
    destination = await _resolve_unique_world_entity(
        db,
        world_id=world_id,
        key=str(row["object_key"] or ""),
        require_location=True,
    )
    if subject is None or destination is None:
        return None

    event_type = "move" if predicate in _EXPLICIT_MOVEMENT else "assert_location"
    return CausalCandidate(
        world_id=world_id,
        timeline_id=timeline_id,
        dag_node_id=row["dag_node_id"] if await _node_on_timeline(db, row["dag_node_id"], timeline_id) else None,
        domain_id="world.location",
        event_type=event_type,
        entity_id=subject,
        target_entity_id=destination,
        state_key="location_entity_id",
        value=str(destination),
        occurred_at=row["event_time"],
        source_kind="semantic",
        source_ref=str(claim_id),
        authority_kind="semantic_candidate",
        parameters={
            "allow_latent_transition": bool(allow_latent_transition),
            "epistemic_scope": row["epistemic_scope"],
            "predicate": predicate,
        },
        claim_id=claim_id,
        frame_id=row["frame_id"],
    )


async def evaluate_location_claim(
    db: Database,
    kernel: CausalIntegrityKernel,
    *,
    claim_id: UUID,
    world_id: UUID,
    timeline_id: UUID,
    allow_latent_transition: bool = False,
) -> Optional[CausalEvaluation]:
    """Ask causal compatibility without changing objective state."""
    candidate = await compile_location_candidate(
        db,
        claim_id=claim_id,
        world_id=world_id,
        timeline_id=timeline_id,
        allow_latent_transition=allow_latent_transition,
    )
    if candidate is None:
        return None
    return await kernel.evaluate(candidate)


async def _node_on_timeline(db: Database, node_id: Optional[UUID], timeline_id: UUID) -> bool:
    if node_id is None:
        return False
    row = await db.fetchrow(
        "SELECT 1 FROM aios.dag_node WHERE node_id=$1 AND timeline_id=$2",
        node_id,
        timeline_id,
    )
    return bool(row)


async def _resolve_unique_world_entity(
    db: Database,
    *,
    world_id: UUID,
    key: str,
    require_location: bool,
) -> Optional[UUID]:
    clean = key.strip()
    if not clean:
        return None

    rows = await db.fetch(
        """
        SELECT entity_id
        FROM aios.world_entity
        WHERE world_id=$1
          AND (
                lower(COALESCE(entity_key,''))=lower($2)
                OR lower(COALESCE(display_name,''))=lower($2)
              )
          AND ($3::boolean=false OR lower(entity_type) IN ('location','room','place','region','world'))
        ORDER BY CASE WHEN lower(COALESCE(entity_key,''))=lower($2) THEN 0 ELSE 1 END,
                 created_at
        LIMIT 2
        """,
        world_id,
        clean,
        require_location,
    )
    if len(rows) != 1:
        # Ambiguous entity linking remains semantic ambiguity.  Never guess a
        # deterministic subject/target just to make a candidate fit.
        return None
    return rows[0]["entity_id"]


async def admit_location_claim(
    db: Database,
    kernel: CausalIntegrityKernel,
    *,
    claim_id: UUID,
    world_id: UUID,
    timeline_id: UUID,
) -> Optional[dict]:
    """Admit only explicitly world-scoped, resolved spatial transitions.

    Character/speaker/source/narrative uncertainty remains epistemic. Semantic
    confidence is deliberately not an authority signal. Ordinary endpoint
    assertions are evaluated but never promoted here; only explicit movement
    predicates can propose a /world transition.
    """
    policy = await db.fetchrow(
        """SELECT ccr.epistemic_scope,ccr.claim_kind,ccr.predicate_family,
                  ccr.world_id,ccr.timeline_id,
                  COALESCE(sf.predicate_canonical,cc.predicate) AS predicate,
                  sf.resolution_status
           FROM aios.claim_candidate cc
           JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
           JOIN aios.claim_semantic_frame_projection sfp ON sfp.claim_id=cc.claim_id
           JOIN aios.claim_semantic_frame sf ON sf.frame_id=sfp.primary_frame_id
           WHERE cc.claim_id=$1""",claim_id)
    if not policy:
        return None
    scope=str(policy["epistemic_scope"] or "").lower()
    predicate=str(policy["predicate"] or "").strip().lower().replace(" ","_")
    resolution=str(policy["resolution_status"] or "").lower()
    if scope != "world":
        return None
    if policy["world_id"] and policy["world_id"] != world_id:
        return None
    if policy["timeline_id"] and policy["timeline_id"] != timeline_id:
        return None
    if predicate not in _EXPLICIT_MOVEMENT:
        return None
    if resolution and resolution not in {"resolved","exact","linked"}:
        return None

    candidate=await compile_location_candidate(
        db,claim_id=claim_id,world_id=world_id,timeline_id=timeline_id,
        allow_latent_transition=False)
    if candidate is None:
        return None
    result=await kernel.commit(candidate)
    if result.get("committed"):
        from aios_app.world.projection_consequences import WorldProjectionCoordinator
        await WorldProjectionCoordinator(db).world_state_changed(
            world_id=world_id,timeline_id=timeline_id,
            domain_id=candidate.domain_id,entity_id=candidate.entity_id,
            before=result.get("before"),after=result.get("after"))
    return result
