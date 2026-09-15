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
    """Plan memberships without declaring uncertain evidence to be world truth."""
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
        "SELECT aios.ensure_world_reality_context($1) AS reality_context_id",
        world_id,
    )
    if not row:
        return None
    context = await db.fetchrow(
        """
        SELECT reality_context_id, context_key, context_kind, world_id,
               label, status, confidence
        FROM aios.reality_context
        WHERE reality_context_id=$1
        """,
        row["reality_context_id"],
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


async def _upsert_claim_evidence(
    db: Database,
    *,
    claim_id: UUID,
    proposition_id: UUID,
    context: dict[str, Any],
    plan: RealityMembershipPlan,
) -> None:
    await db.execute(
        """
        INSERT INTO aios.proposition_reality_evidence (
            claim_id, proposition_id, reality_context_id,
            membership_status, affinity, confidence, assigned_by, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
        ON CONFLICT (claim_id, reality_context_id) DO UPDATE
        SET proposition_id=EXCLUDED.proposition_id,
            membership_status=EXCLUDED.membership_status,
            affinity=EXCLUDED.affinity,
            confidence=EXCLUDED.confidence,
            assigned_by=EXCLUDED.assigned_by,
            updated_at=now(),
            meta=aios.proposition_reality_evidence.meta || EXCLUDED.meta
        """,
        claim_id,
        proposition_id,
        context["reality_context_id"],
        plan.membership_status,
        plan.affinity,
        plan.confidence,
        REALITY_RESOLVER_VERSION,
        json.dumps(
            {
                "reason": plan.reason,
                "context_role": plan.context_role,
                "resolver_version": REALITY_RESOLVER_VERSION,
            }
        ),
    )


async def _recompute_membership(
    db: Database,
    *,
    proposition_id: UUID,
    context_id: UUID,
) -> Optional[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT membership_status, affinity, confidence, claim_id
        FROM aios.proposition_reality_evidence
        WHERE proposition_id=$1
          AND reality_context_id=$2
        ORDER BY
            CASE membership_status
                WHEN 'member' THEN 4
                WHEN 'compatible' THEN 3
                WHEN 'candidate' THEN 2
                WHEN 'challenged' THEN 1
                ELSE 0
            END DESC,
            affinity DESC,
            confidence DESC,
            claim_id
        """,
        proposition_id,
        context_id,
    )

    if not rows:
        await db.execute(
            """
            DELETE FROM aios.proposition_reality_membership
            WHERE proposition_id=$1
              AND reality_context_id=$2
              AND assigned_by=$3
            """,
            proposition_id,
            context_id,
            REALITY_RESOLVER_VERSION,
        )
        return None

    best = rows[0]
    max_affinity = max(float(r["affinity"] or 0.0) for r in rows)
    max_confidence = max(float(r["confidence"] or 0.0) for r in rows)
    await db.execute(
        """
        INSERT INTO aios.proposition_reality_membership (
            proposition_id, reality_context_id, membership_status,
            affinity, confidence, assigned_by, validation_decision_key, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,NULL,$7::jsonb)
        ON CONFLICT (proposition_id, reality_context_id) DO UPDATE
        SET membership_status=EXCLUDED.membership_status,
            affinity=EXCLUDED.affinity,
            confidence=EXCLUDED.confidence,
            assigned_by=EXCLUDED.assigned_by,
            updated_at=now(),
            meta=aios.proposition_reality_membership.meta || EXCLUDED.meta
        """,
        proposition_id,
        context_id,
        best["membership_status"],
        max_affinity,
        max_confidence,
        REALITY_RESOLVER_VERSION,
        json.dumps(
            {
                "evidence_count": len(rows),
                "resolver_version": REALITY_RESOLVER_VERSION,
            }
        ),
    )

    context = await db.fetchrow(
        """
        SELECT reality_context_id, context_key, context_kind, world_id
        FROM aios.reality_context
        WHERE reality_context_id=$1
        """,
        context_id,
    )
    if not context:
        return None
    return {
        "reality_context_id": context["reality_context_id"],
        "context_key": context["context_key"],
        "context_kind": context["context_kind"],
        "world_id": context["world_id"],
        "membership_status": best["membership_status"],
        "affinity": max_affinity,
        "confidence": max_confidence,
        "evidence_count": len(rows),
    }


async def resolve_claim_reality(
    db: Database,
    row: dict[str, Any],
) -> Optional[RealityResolution]:
    """Materialize revisable reality hypotheses for one normalized claim.

    Claim-level evidence is preserved independently. Proposition-level reality
    membership is recomputed from all surviving evidence for each context. This
    function never inserts world_proposition_assertion rows.
    """
    proposition_id = _as_uuid(row.get("proposition_id"))
    claim_id = _as_uuid(row.get("claim_id"))
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

    old_context_ids: set[UUID] = set()
    if claim_id is not None:
        old_rows = await db.fetch(
            """
            SELECT reality_context_id
            FROM aios.proposition_reality_evidence
            WHERE claim_id=$1 AND assigned_by=$2
            """,
            claim_id,
            REALITY_RESOLVER_VERSION,
        )
        old_context_ids = {r["reality_context_id"] for r in old_rows}
        await db.execute(
            """
            DELETE FROM aios.proposition_reality_evidence
            WHERE claim_id=$1 AND assigned_by=$2
            """,
            claim_id,
            REALITY_RESOLVER_VERSION,
        )

    world_id = _candidate_world_id(row)
    source_context: Optional[dict[str, Any]] = None
    world_context: Optional[dict[str, Any]] = None
    new_context_ids: set[UUID] = set()

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

        context_id = context["reality_context_id"]
        new_context_ids.add(context_id)
        if claim_id is not None:
            await _upsert_claim_evidence(
                db,
                claim_id=claim_id,
                proposition_id=proposition_id,
                context=context,
                plan=plan,
            )
        else:
            # Compatibility path for callers lacking a claim coordinate. It is
            # intentionally rare; normal pipeline calls always carry claim_id.
            await db.execute(
                """
                INSERT INTO aios.proposition_reality_membership (
                    proposition_id, reality_context_id, membership_status,
                    affinity, confidence, assigned_by, meta
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                ON CONFLICT (proposition_id, reality_context_id) DO UPDATE
                SET membership_status=EXCLUDED.membership_status,
                    affinity=GREATEST(aios.proposition_reality_membership.affinity, EXCLUDED.affinity),
                    confidence=GREATEST(aios.proposition_reality_membership.confidence, EXCLUDED.confidence),
                    updated_at=now(),
                    meta=aios.proposition_reality_membership.meta || EXCLUDED.meta
                """,
                proposition_id,
                context_id,
                plan.membership_status,
                plan.affinity,
                plan.confidence,
                REALITY_RESOLVER_VERSION,
                json.dumps({"reason": plan.reason, "resolver_version": REALITY_RESOLVER_VERSION}),
            )

    affected_context_ids = old_context_ids | new_context_ids
    memberships: list[dict[str, Any]] = []
    if claim_id is not None:
        for context_id in affected_context_ids:
            materialized = await _recompute_membership(
                db,
                proposition_id=proposition_id,
                context_id=context_id,
            )
            if materialized and context_id in new_context_ids:
                memberships.append(materialized)
    else:
        rows = await db.fetch(
            """
            SELECT prm.reality_context_id, rc.context_key, rc.context_kind, rc.world_id,
                   prm.membership_status, prm.affinity, prm.confidence
            FROM aios.proposition_reality_membership prm
            JOIN aios.reality_context rc ON rc.reality_context_id=prm.reality_context_id
            WHERE prm.proposition_id=$1
              AND prm.reality_context_id=ANY($2::uuid[])
            """,
            proposition_id,
            list(new_context_ids),
        )
        memberships = [dict(r) for r in rows]

    if source_context and world_context:
        world_membership = next(
            (m for m in memberships if m["reality_context_id"] == world_context["reality_context_id"]),
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
                SET affinity=GREATEST(aios.reality_context_edge.affinity, EXCLUDED.affinity),
                    traversal_cost=LEAST(aios.reality_context_edge.traversal_cost, EXCLUDED.traversal_cost),
                    confidence=GREATEST(aios.reality_context_edge.confidence, EXCLUDED.confidence),
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
