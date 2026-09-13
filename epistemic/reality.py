from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

from aios_app.db import Database


REALITY_RESOLVER_VERSION = "reality-hypothesis-v1"
_RUNTIME_SOURCE_KINDS = {
    "chat",
    "chat_message",
    "conversation",
    "roleplay",
    "runtime",
    "sillytavern",
}


@dataclass(frozen=True)
class RealityMembershipPlan:
    context_role: str
    membership_status: str
    affinity: float
    confidence: float
    reason: str


@dataclass(frozen=True)
class RealityResolution:
    proposition_id: UUID
    selected_world_id: Optional[UUID]
    top_context_key: Optional[str]
    memberships: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def world_assignment_verified(self) -> bool:
        return self.selected_world_id is not None


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _as_uuid(value: object) -> Optional[UUID]:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _candidate_world_id(row: dict[str, Any]) -> Optional[UUID]:
    """Choose the concrete-world candidate without letting a hint override runtime truth."""
    scope = _norm(row.get("epistemic_scope"))
    source_kind = _norm(row.get("source_kind"))
    source_id = row.get("source_id")

    if scope in {"world", "observation", "character", "speaker"}:
        return _as_uuid(row.get("world_id"))

    if scope == "narrative" and (not source_id or source_kind in _RUNTIME_SOURCE_KINDS):
        return _as_uuid(row.get("world_id"))

    return _as_uuid(row.get("target_world_id")) or _as_uuid(row.get("world_id"))


def _source_context_key(source_id: str, topic_key: object) -> str:
    topic = str(topic_key or "").strip()
    if not topic:
        return f"source:{source_id}"
    digest = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:16]
    return f"source:{source_id}:topic:{digest}"


def plan_reality_memberships(row: dict[str, Any]) -> tuple[RealityMembershipPlan, ...]:
    """Plan memberships without declaring uncertain evidence to be world truth.

    `world` means the concrete aios.world-backed reality context. `source` means
    a source-implied, topic-local interpretation. Multiple plans can coexist.
    Only a concrete `member` with authoritative confidence is eligible for the
    world-assignment authority boundary.
    """
    scope = _norm(row.get("epistemic_scope"))
    source_kind = _norm(row.get("source_kind"))
    source_id = row.get("source_id")
    world_id = _candidate_world_id(row)

    plans: list[RealityMembershipPlan] = []

    if source_id and scope in {"source", "narrative"}:
        source_affinity = 0.98 if scope == "source" else 0.92
        plans.append(
            RealityMembershipPlan(
                context_role="source",
                membership_status="member",
                affinity=source_affinity,
                confidence=0.95,
                reason="source_implied_reality",
            )
        )

    if not world_id:
        return tuple(plans)

    if scope in {"world", "observation"}:
        plans.append(
            RealityMembershipPlan(
                context_role="world",
                membership_status="member",
                affinity=1.0,
                confidence=1.0,
                reason="explicit_authoritative_world_scope",
            )
        )
    elif scope == "narrative" and (not source_id or source_kind in _RUNTIME_SOURCE_KINDS):
        plans.append(
            RealityMembershipPlan(
                context_role="world",
                membership_status="member",
                affinity=0.98,
                confidence=0.98,
                reason="runtime_narrative_world_scope",
            )
        )
    elif scope == "narrative":
        plans.append(
            RealityMembershipPlan(
                context_role="world",
                membership_status="candidate",
                affinity=0.65,
                confidence=0.65,
                reason="external_narrative_world_hypothesis",
            )
        )
    elif scope == "source":
        plans.append(
            RealityMembershipPlan(
                context_role="world",
                membership_status="candidate",
                affinity=0.55,
                confidence=0.55,
                reason="source_target_world_hypothesis",
            )
        )
    elif scope in {"character", "speaker"}:
        plans.append(
            RealityMembershipPlan(
                context_role="world",
                membership_status="compatible",
                affinity=0.90,
                confidence=0.85,
                reason="character_perspective_world_compatibility",
            )
        )
    else:
        plans.append(
            RealityMembershipPlan(
                context_role="world",
                membership_status="candidate",
                affinity=0.50,
                confidence=0.45,
                reason="context_world_hypothesis",
            )
        )

    return tuple(plans)


async def _ensure_world_context(db: Database, world_id: UUID) -> Optional[dict[str, Any]]:
    # target_world_id is only a hint for external ingest. A stale or mistyped
    # optional hint must not make the semantic topology job fail.
    exists = await db.fetchrow(
        "SELECT world_id FROM aios.world WHERE world_id=$1",
        world_id,
    )
    if not exists:
        return None

    row = await db.fetchrow(
        """
        SELECT aios.ensure_world_reality_context($1) AS reality_context_id
        """,
        world_id,
    )
    if not row:
        return None
    context_id = row["reality_context_id"]
    context = await db.fetchrow(
        """
        SELECT reality_context_id, context_key, context_kind, world_id,
               label, status, confidence
        FROM aios.reality_context
        WHERE reality_context_id=$1
        """,
        context_id,
    )
    return dict(context) if context else None


async def _ensure_source_context(db: Database, row: dict[str, Any]) -> dict[str, Any]:
    source_id = str(row["source_id"])
    topic_key = row.get("topic_key")
    context_key = _source_context_key(source_id, topic_key)
    label = source_id if not topic_key else f"{source_id} · {topic_key}"
    created = await db.fetchrow(
        """
        INSERT INTO aios.reality_context (
            context_key, context_kind, topic_key, label, status,
            coherence, confidence, meta
        )
        VALUES ($1,'source_implied',$2,$3,'open',NULL,0.5,$4::jsonb)
        ON CONFLICT (context_key) DO UPDATE
        SET topic_key=COALESCE(EXCLUDED.topic_key, aios.reality_context.topic_key),
            label=COALESCE(aios.reality_context.label, EXCLUDED.label),
            updated_at=now(),
            meta=aios.reality_context.meta || EXCLUDED.meta
        RETURNING reality_context_id, context_key, context_kind, world_id,
                  label, status, confidence
        """,
        context_key,
        str(topic_key) if topic_key else None,
        label,
        json.dumps(
            {
                "source_id": source_id,
                "source_kind": row.get("source_kind"),
                "resolver_version": REALITY_RESOLVER_VERSION,
            }
        ),
    )
    return dict(created)


async def _upsert_membership(
    db: Database,
    *,
    proposition_id: UUID,
    context: dict[str, Any],
    plan: RealityMembershipPlan,
    claim_id: object,
) -> dict[str, Any]:
    await db.execute(
        """
        INSERT INTO aios.proposition_reality_membership (
            proposition_id, reality_context_id, membership_status,
            affinity, confidence, assigned_by, validation_decision_key, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
        ON CONFLICT (proposition_id, reality_context_id) DO UPDATE
        SET membership_status=EXCLUDED.membership_status,
            affinity=EXCLUDED.affinity,
            confidence=EXCLUDED.confidence,
            assigned_by=EXCLUDED.assigned_by,
            validation_decision_key=EXCLUDED.validation_decision_key,
            updated_at=now(),
            meta=aios.proposition_reality_membership.meta || EXCLUDED.meta
        """,
        proposition_id,
        context["reality_context_id"],
        plan.membership_status,
        plan.affinity,
        plan.confidence,
        REALITY_RESOLVER_VERSION,
        str(claim_id) if claim_id else None,
        json.dumps(
            {
                "reason": plan.reason,
                "context_role": plan.context_role,
                "resolver_version": REALITY_RESOLVER_VERSION,
            }
        ),
    )
    return {
        "reality_context_id": context["reality_context_id"],
        "context_key": context["context_key"],
        "context_kind": context["context_kind"],
        "world_id": context.get("world_id"),
        "membership_status": plan.membership_status,
        "affinity": plan.affinity,
        "confidence": plan.confidence,
        "reason": plan.reason,
    }


async def resolve_claim_reality(
    db: Database,
    row: dict[str, Any],
) -> Optional[RealityResolution]:
    """Materialize revisable reality hypotheses for one normalized claim.

    This function does not insert world_proposition_assertion rows. A concrete
    world assignment is returned only for authoritative runtime/world evidence;
    source and external narrative evidence remains candidate/compatible state.
    """
    proposition_id = _as_uuid(row.get("proposition_id"))
    if proposition_id is None:
        return None

    plans = plan_reality_memberships(row)
    if not plans:
        return RealityResolution(
            proposition_id=proposition_id,
            selected_world_id=None,
            top_context_key=None,
            memberships=(),
        )

    await db.execute(
        """
        DELETE FROM aios.proposition_reality_membership
        WHERE proposition_id=$1
          AND assigned_by=$2
        """,
        proposition_id,
        REALITY_RESOLVER_VERSION,
    )

    world_id = _candidate_world_id(row)
    source_context: Optional[dict[str, Any]] = None
    world_context: Optional[dict[str, Any]] = None
    memberships: list[dict[str, Any]] = []

    for plan in plans:
        if plan.context_role == "source":
            if not row.get("source_id"):
                continue
            if source_context is None:
                source_context = await _ensure_source_context(db, row)
            context = source_context
        elif plan.context_role == "world":
            if world_id is None:
                continue
            if world_context is None:
                world_context = await _ensure_world_context(db, world_id)
            if world_context is None:
                continue
            context = world_context
        else:
            continue

        memberships.append(
            await _upsert_membership(
                db,
                proposition_id=proposition_id,
                context=context,
                plan=plan,
                claim_id=row.get("claim_id"),
            )
        )

    if source_context and world_context:
        world_membership = next(
            (m for m in memberships if m["context_key"] == world_context["context_key"]),
            None,
        )
        if world_membership:
            await db.execute(
                """
                INSERT INTO aios.reality_context_edge (
                    from_context_id, to_context_id, relation_type,
                    affinity, traversal_cost, confidence, meta
                )
                VALUES ($1,$2,'interprets',$3,$4,$5,$6::jsonb)
                ON CONFLICT (from_context_id, to_context_id, relation_type) DO UPDATE
                SET affinity=EXCLUDED.affinity,
                    traversal_cost=EXCLUDED.traversal_cost,
                    confidence=EXCLUDED.confidence,
                    updated_at=now(),
                    meta=aios.reality_context_edge.meta || EXCLUDED.meta
                """,
                source_context["reality_context_id"],
                world_context["reality_context_id"],
                float(world_membership["affinity"]),
                max(0.0, min(1.0, 1.0 - float(world_membership["affinity"]))),
                float(world_membership["confidence"]),
                json.dumps(
                    {
                        "claim_id": str(row.get("claim_id")) if row.get("claim_id") else None,
                        "resolver_version": REALITY_RESOLVER_VERSION,
                    }
                ),
            )

    ranked = sorted(
        memberships,
        key=lambda item: (float(item["affinity"]), float(item["confidence"])),
        reverse=True,
    )
    top_context_key = ranked[0]["context_key"] if ranked else None

    selected_world_id: Optional[UUID] = None
    for membership in memberships:
        if (
            membership.get("world_id") is not None
            and membership["membership_status"] == "member"
            and float(membership["affinity"]) >= 0.95
            and float(membership["confidence"]) >= 0.95
        ):
            selected_world_id = _as_uuid(membership["world_id"])
            break

    return RealityResolution(
        proposition_id=proposition_id,
        selected_world_id=selected_world_id,
        top_context_key=top_context_key,
        memberships=tuple(memberships),
    )
