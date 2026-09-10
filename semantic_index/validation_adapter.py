from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aios_app.epistemic.hypothesis_validation import (
    MatrixOutcome,
    notify_evidence_change,
    record_validation_decision,
)
from aios_app.semantic_index.relation_validator import (
    RELATION_VERIFIER_VERSION,
    validate_neighbor_relation,
)


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


async def record_new_relation_decisions(db, *, limit: int = 500) -> int:
    rows = await db.fetch(
        """
        SELECT r.proposition_id, r.neighbor_proposition_id, r.relation,
               r.confidence, r.features
        FROM aios.semantic_neighbor_relation r
        WHERE r.features->>'verifier_version'=$1
          AND NOT EXISTS (
              SELECT 1 FROM aios.semantic_validation_decision d
              WHERE d.decision_type IN ('proposition_relation','event_identity')
                AND d.decision_key=(r.proposition_id::text || ':' || r.neighbor_proposition_id::text)
                AND d.status <> 'stale'
          )
        ORDER BY r.created_at
        LIMIT $2
        """,
        RELATION_VERIFIER_VERSION,
        limit,
    )
    written = 0
    for row in rows:
        features = _json_object(row["features"])
        verification = _json_object(features.get("adversarial_verification"))
        relation = str(row["relation"])
        dtype = "event_identity" if relation == "SAME_EVENT" or features.get("both_events") else "proposition_relation"
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
        written += 1
    return written


async def validated_neighbor_classifier(original, db, cfg) -> int:
    written = await original(db, cfg)
    await record_new_relation_decisions(db, limit=max(100, cfg.batch_size * 4))
    return written
