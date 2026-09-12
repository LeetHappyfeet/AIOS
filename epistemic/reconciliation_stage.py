from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.belief_reconciliation import reconcile_character_belief_atom
from aios_app.epistemic.context_resolver import classify_predicate_family
from aios_app.epistemic.reconciliation_policy import (
    POLICY_ENGINE_VERSION,
    EvidencePoint,
    aggregate_support,
    choose_stance,
    policy_for_family,
)


RECONCILIATION_STAGE_VERSION = "memory-reconciliation-v2"


@dataclass(frozen=True)
class ReconciliationResult:
    proposition_id: UUID
    atom_id: UUID
    path: str
    scope_key: str
    outcome: str
    detail: dict[str, Any]
    claim_id: UUID | None = None
    assertion_id: UUID | None = None


async def _record_claim_receipt(
    db: Database,
    *,
    claim_id: UUID,
    proposition_id: UUID,
    atom_id: UUID,
    path: str,
    scope_key: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO aios.memory_reconciliation_receipt (
            claim_id, assertion_id, proposition_id, atom_id, path, scope_key,
            outcome, resolver_version, meta, reconciled_at, updated_at
        )
        VALUES ($1,NULL,$2,$3,$4,$5,$6,$7,$8::jsonb,now(),now())
        ON CONFLICT (claim_id) DO UPDATE
        SET proposition_id=EXCLUDED.proposition_id,
            atom_id=EXCLUDED.atom_id,
            path=EXCLUDED.path,
            scope_key=EXCLUDED.scope_key,
            outcome=EXCLUDED.outcome,
            resolver_version=EXCLUDED.resolver_version,
            meta=EXCLUDED.meta,
            reconciled_at=now(),
            updated_at=now()
        """,
        claim_id,
        proposition_id,
        atom_id,
        path,
        scope_key,
        outcome,
        RECONCILIATION_STAGE_VERSION,
        json.dumps(detail or {}),
    )


async def _record_assertion_receipt(
    db: Database,
    *,
    assertion_id: UUID,
    proposition_id: UUID,
    atom_id: UUID,
    world_id: UUID,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO aios.memory_reconciliation_receipt (
            claim_id, assertion_id, proposition_id, atom_id, path, scope_key,
            outcome, resolver_version, meta, reconciled_at, updated_at
        )
        VALUES (NULL,$1,$2,$3,'world',$4,$5,$6,$7::jsonb,now(),now())
        ON CONFLICT (assertion_id) DO UPDATE
        SET proposition_id=EXCLUDED.proposition_id,
            atom_id=EXCLUDED.atom_id,
            path=EXCLUDED.path,
            scope_key=EXCLUDED.scope_key,
            outcome=EXCLUDED.outcome,
            resolver_version=EXCLUDED.resolver_version,
            meta=EXCLUDED.meta,
            reconciled_at=now(),
            updated_at=now()
        """,
        assertion_id,
        proposition_id,
        atom_id,
        f"world:{world_id}",
        outcome,
        RECONCILIATION_STAGE_VERSION,
        json.dumps(detail or {}),
    )


def _order_value(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _world_spatial_winner(
    db: Database,
    *,
    world_id: UUID,
    subject_norm: str | None,
) -> UUID | None:
    if not subject_norm:
        return None
    rows = await db.fetch(
        """
        SELECT
            p.atom_id, p.predicate_norm,
            COALESCE(dn.created_at, a.updated_at, a.created_at) AS evidence_time,
            a.assertion_id
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        LEFT JOIN aios.dag_node dn ON dn.node_id=a.generated_at_node_id
        WHERE a.world_id=$1
          AND p.subject_norm=$2
          AND a.epistemic_status NOT IN ('rejected','superseded')
        ORDER BY COALESCE(dn.created_at, a.updated_at, a.created_at) DESC,
                 a.assertion_id DESC
        """,
        world_id,
        subject_norm,
    )
    for row in rows:
        if classify_predicate_family(row["predicate_norm"]) == "SPATIAL":
            return row["atom_id"]
    return None


async def _reconcile_world_atom(
    db: Database,
    *,
    world_id: UUID,
    atom_id: UUID,
) -> dict[str, Any]:
    """Reconcile explicit /world assertions through the semantic policy engine.

    world_proposition_assertion remains the authority boundary. Narrative/source
    observations and character acquisitions cannot silently become objective
    state. Policies only decide how already-explicit world assertions converge.
    """

    rows = await db.fetch(
        """
        SELECT
            a.assertion_id,
            a.proposition_id,
            a.source_kind,
            a.generated_at_node_id,
            a.created_at,
            a.updated_at,
            p.polarity,
            p.subject_norm,
            p.predicate_norm,
            LEAST(0.999999, GREATEST(0.0, COALESCE(a.confidence, 0.0))) AS weight,
            COALESCE(dn.created_at, a.updated_at, a.created_at) AS evidence_time
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        LEFT JOIN aios.dag_node dn ON dn.node_id=a.generated_at_node_id
        WHERE a.world_id=$1
          AND p.atom_id=$2
          AND a.epistemic_status NOT IN ('rejected','superseded')
        ORDER BY COALESCE(dn.created_at, a.updated_at, a.created_at) DESC,
                 a.assertion_id DESC
        """,
        world_id,
        atom_id,
    )

    if not rows:
        await db.execute(
            "DELETE FROM aios.world_memory_state WHERE world_id=$1 AND atom_id=$2",
            world_id,
            atom_id,
        )
        return {"status": "empty"}

    family = classify_predicate_family(rows[0]["predicate_norm"])
    policy = policy_for_family(family)

    if policy.exclusive_slot:
        winner = await _world_spatial_winner(
            db,
            world_id=world_id,
            subject_norm=rows[0]["subject_norm"],
        )
        if winner is not None and winner != atom_id:
            await db.execute(
                """
                INSERT INTO aios.world_memory_state (
                    world_id, atom_id, stance,
                    positive_support, negative_support, state_confidence,
                    preferred_proposition_id, preferred_assertion_id,
                    evidence_count, independent_evidence_count,
                    resolved_through_node_id, resolver_version, meta,
                    resolved_at, updated_at
                )
                VALUES ($1,$2,'unresolved',0.0,0.0,0.0,$3,$4,$5,0,$6,$7,$8::jsonb,now(),now())
                ON CONFLICT (world_id, atom_id) DO UPDATE
                SET stance='unresolved', positive_support=0.0, negative_support=0.0,
                    state_confidence=0.0,
                    preferred_proposition_id=EXCLUDED.preferred_proposition_id,
                    preferred_assertion_id=EXCLUDED.preferred_assertion_id,
                    evidence_count=EXCLUDED.evidence_count,
                    independent_evidence_count=0,
                    resolved_through_node_id=EXCLUDED.resolved_through_node_id,
                    resolver_version=EXCLUDED.resolver_version,
                    meta=EXCLUDED.meta, resolved_at=now(), updated_at=now()
                """,
                world_id,
                atom_id,
                rows[0]["proposition_id"],
                rows[0]["assertion_id"],
                len(rows),
                rows[0]["generated_at_node_id"],
                RECONCILIATION_STAGE_VERSION,
                json.dumps({
                    "path": "world",
                    "authority_boundary": "world_proposition_assertion",
                    "evidence_topology_preserved": True,
                    "policy_engine_version": POLICY_ENGINE_VERSION,
                    "predicate_family": family,
                    "policy_name": policy.name,
                    "policy_mode": policy.mode,
                    "state": "historical_not_current",
                    "slot_winner_atom_id": str(winner),
                }),
            )
            return {
                "status": "materialized",
                "stance": "unresolved",
                "positive_support": 0.0,
                "negative_support": 0.0,
                "evidence_count": len(rows),
                "independent_evidence_count": 0,
                "predicate_family": family,
                "policy": policy.name,
                "state": "historical_not_current",
            }

    evidence = []
    for row in rows:
        # generated_at_node_id is a better event coordinate than assertion_id.
        # If no node exists, collapse repeated source-kind assertions of the
        # same proposition rather than treating every row as a new witness.
        correlation_key = (
            f"{row['source_kind'] or 'unknown'}:node:{row['generated_at_node_id']}"
            if row["generated_at_node_id"] is not None
            else f"{row['source_kind'] or 'unknown'}:proposition:{row['proposition_id']}"
        )
        evidence.append(
            EvidencePoint(
                polarity=int(row["polarity"] or 1),
                weight=float(row["weight"] or 0.0),
                order=_order_value(row["evidence_time"]),
                correlation_key=correlation_key,
            )
        )

    positive, negative, independent_count = aggregate_support(evidence, policy)
    stance, confidence = choose_stance(positive, negative, policy)

    if policy.mode == "latest":
        preferred = max(rows, key=lambda r: (_order_value(r["evidence_time"]), float(r["weight"] or 0.0)))
    elif policy.mode == "max":
        preferred = max(rows, key=lambda r: (float(r["weight"] or 0.0), _order_value(r["evidence_time"])))
    else:
        preferred_polarity = 1 if stance == "positive" else -1 if stance == "negative" else None
        candidates = [r for r in rows if preferred_polarity is None or int(r["polarity"] or 1) == preferred_polarity]
        preferred = max(candidates or rows, key=lambda r: (float(r["weight"] or 0.0), _order_value(r["evidence_time"])))

    await db.execute(
        """
        INSERT INTO aios.world_memory_state (
            world_id, atom_id, stance,
            positive_support, negative_support, state_confidence,
            preferred_proposition_id, preferred_assertion_id,
            evidence_count, independent_evidence_count,
            resolved_through_node_id, resolver_version, meta,
            resolved_at, updated_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,now(),now())
        ON CONFLICT (world_id, atom_id) DO UPDATE
        SET stance=EXCLUDED.stance,
            positive_support=EXCLUDED.positive_support,
            negative_support=EXCLUDED.negative_support,
            state_confidence=EXCLUDED.state_confidence,
            preferred_proposition_id=EXCLUDED.preferred_proposition_id,
            preferred_assertion_id=EXCLUDED.preferred_assertion_id,
            evidence_count=EXCLUDED.evidence_count,
            independent_evidence_count=EXCLUDED.independent_evidence_count,
            resolved_through_node_id=EXCLUDED.resolved_through_node_id,
            resolver_version=EXCLUDED.resolver_version,
            meta=EXCLUDED.meta,
            resolved_at=now(),
            updated_at=now()
        """,
        world_id,
        atom_id,
        stance,
        positive,
        negative,
        confidence,
        preferred["proposition_id"],
        preferred["assertion_id"],
        len(rows),
        independent_count,
        preferred["generated_at_node_id"],
        RECONCILIATION_STAGE_VERSION,
        json.dumps({
            "path": "world",
            "authority_boundary": "world_proposition_assertion",
            "evidence_topology_preserved": True,
            "policy_engine_version": POLICY_ENGINE_VERSION,
            "predicate_family": family,
            "policy_name": policy.name,
            "policy_mode": policy.mode,
            "exclusive_slot": policy.exclusive_slot,
        }),
    )

    return {
        "status": "materialized",
        "stance": stance,
        "positive_support": positive,
        "negative_support": negative,
        "evidence_count": len(rows),
        "independent_evidence_count": independent_count,
        "predicate_family": family,
        "policy": policy.name,
    }


async def reconcile_claim_memory(db: Database, *, claim_id: UUID) -> ReconciliationResult:
    """Route one normalized claim without allowing implicit /world promotion."""

    row = await db.fetchrow(
        """
        SELECT
            o.claim_id,
            o.proposition_id,
            p.atom_id,
            ccr.epistemic_scope,
            ccr.world_id,
            ccr.source_id,
            ccr.origin_character_id,
            ccr.character_instance_id,
            ccr.confidence AS context_confidence
        FROM aios.observation o
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
        WHERE o.claim_id=$1
        """,
        claim_id,
    )
    if not row:
        raise RuntimeError(
            f"Cannot reconcile claim {claim_id}: normalized observation/context missing"
        )

    proposition_id = row["proposition_id"]
    atom_id = row["atom_id"]
    scope = str(row["epistemic_scope"] or "unresolved")

    if scope == "character" and row["character_instance_id"] is not None:
        instance_id = row["character_instance_id"]
        await reconcile_character_belief_atom(db, instance_id=instance_id, atom_id=atom_id)
        scope_key = f"char:{row['origin_character_id'] or instance_id}"
        detail = {
            "instance_id": str(instance_id),
            "context_confidence": float(row["context_confidence"] or 0.0),
            "world_promotion": False,
            "policy_engine_version": POLICY_ENGINE_VERSION,
        }
        outcome = "character_belief_reconciled"
        path = "char"
    else:
        if row["source_id"] is not None:
            scope_key = f"source:{row['source_id']}"
        elif scope == "narrative" and row["world_id"] is not None:
            scope_key = f"world:{row['world_id']}:narrative-evidence"
        elif row["world_id"] is not None:
            scope_key = f"world:{row['world_id']}:evidence"
        else:
            scope_key = f"claim:{claim_id}"
        detail = {
            "epistemic_scope": scope,
            "reason": "explicit_world_assertion_required",
            "context_confidence": float(row["context_confidence"] or 0.0),
            "world_promotion": False,
            "policy_engine_version": POLICY_ENGINE_VERSION,
        }
        outcome = "evidence_only"
        path = "evidence"

    await _record_claim_receipt(
        db,
        claim_id=claim_id,
        proposition_id=proposition_id,
        atom_id=atom_id,
        path=path,
        scope_key=scope_key,
        outcome=outcome,
        detail=detail,
    )

    return ReconciliationResult(
        claim_id=claim_id,
        assertion_id=None,
        proposition_id=proposition_id,
        atom_id=atom_id,
        path=path,
        scope_key=scope_key,
        outcome=outcome,
        detail=detail,
    )


async def reconcile_world_assertion(
    db: Database,
    *,
    assertion_id: UUID,
) -> ReconciliationResult:
    """Reconcile one explicit world assertion into /world current memory."""

    row = await db.fetchrow(
        """
        SELECT
            a.assertion_id,
            a.world_id,
            a.proposition_id,
            a.epistemic_status,
            a.source_kind,
            a.confidence,
            p.atom_id
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        WHERE a.assertion_id=$1
        """,
        assertion_id,
    )
    if not row:
        raise RuntimeError(f"Cannot reconcile missing world assertion {assertion_id}")

    world_id = row["world_id"]
    atom_id = row["atom_id"]
    detail = await _reconcile_world_atom(db, world_id=world_id, atom_id=atom_id)
    detail = {
        **detail,
        "authority_boundary": "world_proposition_assertion",
        "epistemic_status": row["epistemic_status"],
        "source_kind": row["source_kind"],
        "confidence": float(row["confidence"] or 0.0),
    }
    outcome = (
        "world_assertion_excluded"
        if row["epistemic_status"] in {"rejected", "superseded"}
        else "world_memory_reconciled"
    )

    await _record_assertion_receipt(
        db,
        assertion_id=assertion_id,
        proposition_id=row["proposition_id"],
        atom_id=atom_id,
        world_id=world_id,
        outcome=outcome,
        detail=detail,
    )

    return ReconciliationResult(
        claim_id=None,
        assertion_id=assertion_id,
        proposition_id=row["proposition_id"],
        atom_id=atom_id,
        path="world",
        scope_key=f"world:{world_id}",
        outcome=outcome,
        detail=detail,
    )
