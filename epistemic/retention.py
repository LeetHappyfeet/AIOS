from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from aios_app.db import Database


RETENTION_VERSION = "semantic-retention-v1"


class RetentionState(str, Enum):
    ACTIVE = "ACTIVE"
    COLD = "COLD"
    QUARANTINED = "QUARANTINED"
    TRASHED = "TRASHED"


@dataclass(frozen=True)
class RetentionDecision:
    artifact_type: str
    artifact_id: str
    state: RetentionState
    reason_code: str
    protected: bool
    detail: dict[str, Any]


async def set_retention_state(
    db: Database,
    *,
    artifact_type: str,
    artifact_id: UUID | str,
    state: RetentionState | str,
    reason_code: str,
    utility_score: float | None = None,
    quality_score: float | None = None,
    redundancy_score: float | None = None,
    recoverability: float | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Request a logical retention transition.

    Phase 1 never physically deletes the artifact. The database function owns
    dependency protection and reconciliation invalidation so every caller uses
    the same safety rules.
    """

    normalized_state = RetentionState(state).value
    row = await db.fetchrow(
        """
        SELECT aios.set_semantic_retention_state(
            $1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb
        ) AS changed
        """,
        artifact_type,
        str(artifact_id),
        normalized_state,
        reason_code,
        utility_score,
        quality_score,
        redundancy_score,
        recoverability,
        meta or {},
    )
    return bool(row and row["changed"])


async def evaluate_acquisition_retention(
    db: Database,
    *,
    acquisition_id: UUID,
) -> RetentionDecision:
    """Run the deterministic Phase-1 evaluator for one /char evidence item."""

    await db.execute(
        "SELECT aios.evaluate_acquisition_retention($1)",
        acquisition_id,
    )
    return await get_retention_decision(
        db,
        artifact_type="knowledge_acquisition_event",
        artifact_id=acquisition_id,
    )


async def get_retention_decision(
    db: Database,
    *,
    artifact_type: str,
    artifact_id: UUID | str,
) -> RetentionDecision:
    row = await db.fetchrow(
        """
        SELECT state, reason_code, protected, meta
        FROM aios.semantic_retention_state
        WHERE artifact_type=$1 AND artifact_id=$2
        """,
        artifact_type,
        str(artifact_id),
    )
    if not row:
        return RetentionDecision(
            artifact_type=artifact_type,
            artifact_id=str(artifact_id),
            state=RetentionState.ACTIVE,
            reason_code="implicit_active",
            protected=False,
            detail={},
        )
    meta = row["meta"]
    if isinstance(meta, str):
        import json
        meta = json.loads(meta)
    return RetentionDecision(
        artifact_type=artifact_type,
        artifact_id=str(artifact_id),
        state=RetentionState(row["state"]),
        reason_code=row["reason_code"],
        protected=bool(row["protected"]),
        detail=dict(meta or {}),
    )
