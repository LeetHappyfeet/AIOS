from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from aios_app.epistemic.hypothesis_validation import (
    MatrixOutcome,
    notify_evidence_change,
    record_validation_decision,
)
from aios_app.semantic_index.relation_validator import RELATION_VERIFIER_VERSION


def _json_object(value: Any) -> dict[str, Any]:
    """Normalize asyncpg JSON/JSONB values to a Python object mapping."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _pair_ids(decision_key: str) -> tuple[UUID, UUID] | None:
    try:
        left, right = decision_key.split(":", 1)
        return UUID(left), UUID(right)
    except (ValueError, AttributeError):
        return None


async def _record_relation_decision(db, row) -> None:
    features = _json_object(row["features"])
    verification = _json_object(features.get("adversarial_verification"))
    relation = str(row["relation"])
    dtype = (
        "event_identity"
        if relation == "SAME_EVENT" or features.get("both_events")
        else "proposition_relation"
    )
    key = f"{row['proposition_id']}:{row['neighbor_proposition_id']}"
    matrix = _json_object(verification.get("matrix"))
    outcome = MatrixOutcome(
        proposed_key=str(verification.get("proposed_key") or relation),
        winner_key=verification.get("winner_key"),
        winner_score=int(verification.get("winner_score") or 0),
        runner_up_score=int(verification.get("runner_up_score") or 0),
        margin=int(verification.get("margin") or 0),
        stability=float(verification.get("stability") or 0.0),
        status=str(verification.get("status") or "insufficient_evidence"),
        matrix=matrix,
    )
    dependencies = [
        ("semantic_neighbor", key),
        ("proposition", str(row["proposition_id"])),
        ("proposition", str(row["neighbor_proposition_id"])),
        ("proposition_conflict", key),
    ]
    await record_validation_decision(
        db,
        decision_type=dtype,
        decision_key=key,
        subject_type="proposition_pair",
        subject_key=key,
        outcome=outcome,
        selected_value=relation,
        resolver_version=RELATION_VERIFIER_VERSION,
        dependencies=dependencies,
        meta={"confidence": float(row["confidence"] or 0.0)},
    )

    # Neighbor geometry and the accepted relation are different evidence
    # channels. Publish both so ownership/world decisions that depend on
    # geometry can refresh even if the relation itself remains unchanged.
    for proposition_id in (row["proposition_id"], row["neighbor_proposition_id"]):
        await notify_evidence_change(
            db,
            evidence_type="semantic_neighbors",
            evidence_key=str(proposition_id),
        )
    await notify_evidence_change(
        db,
        evidence_type="decision",
        evidence_key=f"{dtype}:{key}",
    )


async def record_new_relation_decisions(db, *, limit: int = 500) -> int:
    """Drain targeted semantic-relation validation work.

    This intentionally does not rediscover work by subtracting the entire
    semantic_validation_decision history from semantic_neighbor_relation.
    Inserts and validation invalidations enqueue exact proposition pairs.
    """
    queued = await db.fetch(
        """
        SELECT decision_type, decision_key, enqueued_at
        FROM aios.semantic_relation_validation_queue
        ORDER BY enqueued_at
        LIMIT $1
        """,
        limit,
    )
    written = 0
    for item in queued:
        pair = _pair_ids(str(item["decision_key"]))
        if pair is None:
            await db.execute(
                """
                DELETE FROM aios.semantic_relation_validation_queue
                WHERE decision_type=$1 AND decision_key=$2 AND enqueued_at=$3
                """,
                item["decision_type"], item["decision_key"], item["enqueued_at"],
            )
            continue

        row = await db.fetchrow(
            """
            SELECT proposition_id, neighbor_proposition_id, relation,
                   confidence, features
            FROM aios.semantic_neighbor_relation
            WHERE proposition_id=$1
              AND neighbor_proposition_id=$2
              AND features->>'verifier_version'=$3
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            pair[0],
            pair[1],
            RELATION_VERIFIER_VERSION,
        )
        if row is None:
            # The classifier receipt may have been superseded/removed after the
            # queue entry was created. There is no decision to reconstruct.
            await db.execute(
                """
                DELETE FROM aios.semantic_relation_validation_queue
                WHERE decision_type=$1 AND decision_key=$2 AND enqueued_at=$3
                """,
                item["decision_type"], item["decision_key"], item["enqueued_at"],
            )
            continue

        features = _json_object(row["features"])
        expected_type = (
            "event_identity"
            if str(row["relation"]) == "SAME_EVENT" or features.get("both_events")
            else "proposition_relation"
        )
        if str(item["decision_type"]) != expected_type:
            # A relation/type transition enqueues its current type separately.
            # Retire only this obsolete queue identity.
            await db.execute(
                """
                DELETE FROM aios.semantic_relation_validation_queue
                WHERE decision_type=$1 AND decision_key=$2 AND enqueued_at=$3
                """,
                item["decision_type"], item["decision_key"], item["enqueued_at"],
            )
            continue

        await _record_relation_decision(db, row)
        # Delete only the queue generation we processed. If evidence changed
        # while validation was running, its refreshed enqueued_at survives.
        await db.execute(
            """
            DELETE FROM aios.semantic_relation_validation_queue
            WHERE decision_type=$1 AND decision_key=$2 AND enqueued_at=$3
            """,
            item["decision_type"], item["decision_key"], item["enqueued_at"],
        )
        written += 1
    return written


async def validated_neighbor_classifier(original, db, cfg) -> int:
    written = await original(db, cfg)
    # New relation inserts enqueue exact pairs through the database trigger.
    # Drain only when this classifier actually produced work; reconciliation
    # also drains the queue, including targeted stale-decision revalidation.
    if written:
        await record_new_relation_decisions(db, limit=getattr(cfg, "validation_batch_size", max(100, cfg.batch_size * 4)))
    return written
