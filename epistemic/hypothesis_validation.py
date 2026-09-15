from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from aios_app.db import Database

VALIDATION_ENGINE_VERSION = "semantic-hypothesis-matrix-v1"


@dataclass(frozen=True)
class MatrixOutcome:
    proposed_key: str
    winner_key: Optional[str]
    winner_score: int
    runner_up_score: int
    margin: int
    stability: float
    status: str
    matrix: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_meta(self) -> dict[str, Any]:
        return {
            "engine_version": VALIDATION_ENGINE_VERSION,
            "proposed_key": self.proposed_key,
            "winner_key": self.winner_key,
            "winner_score": self.winner_score,
            "runner_up_score": self.runner_up_score,
            "margin": self.margin,
            "stability": round(float(self.stability), 6),
            "status": self.status,
            "matrix": self.matrix,
        }


def _totals(matrix: dict[str, dict[str, int]], omit_axis: Optional[str] = None) -> dict[str, int]:
    return {
        candidate: sum(score for axis, score in axes.items() if axis != omit_axis)
        for candidate, axes in matrix.items()
    }


def _rank(totals: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def evaluate_matrix(
    matrix: dict[str, dict[str, int]],
    *,
    proposed_key: str,
    min_score: int = 4,
    min_margin: int = 3,
    min_stability: float = 0.75,
    protected: bool = False,
) -> MatrixOutcome:
    ranked = _rank(_totals(matrix))
    if not ranked:
        return MatrixOutcome(
            proposed_key=proposed_key,
            winner_key=None,
            winner_score=0,
            runner_up_score=0,
            margin=0,
            stability=0.0,
            status="insufficient_evidence",
            matrix=matrix,
        )

    winner_key, winner_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0
    margin = winner_score - runner_up_score
    axes = sorted({axis for values in matrix.values() for axis in values})
    stable = 0
    for axis in axes:
        trial = _rank(_totals(matrix, omit_axis=axis))
        if trial and trial[0][0] == winner_key:
            stable += 1
    stability = stable / len(axes) if axes else 1.0

    if protected:
        status = "verified" if winner_key == proposed_key else "protected_explicit"
    elif winner_key != proposed_key:
        status = "challenged"
    elif winner_score < min_score or margin < min_margin or stability < min_stability:
        status = "fragile"
    else:
        status = "verified"

    return MatrixOutcome(
        proposed_key=proposed_key,
        winner_key=winner_key,
        winner_score=winner_score,
        runner_up_score=runner_up_score,
        margin=margin,
        stability=stability,
        status=status,
        matrix=matrix,
    )


async def record_validation_decision(
    db: Database,
    *,
    decision_type: str,
    decision_key: str,
    subject_type: str,
    subject_key: str,
    outcome: MatrixOutcome,
    selected_value: Optional[str],
    resolver_version: str,
    dependencies: Iterable[tuple[str, str]],
    meta: Optional[dict[str, Any]] = None,
) -> None:
    await db.execute(
        """
        INSERT INTO aios.semantic_validation_decision (
            decision_type, decision_key, subject_type, subject_key,
            proposed_value, selected_value, status, resolver_version,
            winner_score, runner_up_score, margin, stability, matrix, meta,
            evaluated_at, stale_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,now(),NULL)
        ON CONFLICT (decision_type, decision_key) DO UPDATE
        SET subject_type=EXCLUDED.subject_type,
            subject_key=EXCLUDED.subject_key,
            proposed_value=EXCLUDED.proposed_value,
            selected_value=EXCLUDED.selected_value,
            status=EXCLUDED.status,
            resolver_version=EXCLUDED.resolver_version,
            winner_score=EXCLUDED.winner_score,
            runner_up_score=EXCLUDED.runner_up_score,
            margin=EXCLUDED.margin,
            stability=EXCLUDED.stability,
            matrix=EXCLUDED.matrix,
            meta=aios.semantic_validation_decision.meta || EXCLUDED.meta,
            evaluated_at=now(), stale_at=NULL,
            revision=aios.semantic_validation_decision.revision + 1
        """,
        decision_type, decision_key, subject_type, subject_key,
        outcome.proposed_key, selected_value, outcome.status, resolver_version,
        outcome.winner_score, outcome.runner_up_score, outcome.margin,
        outcome.stability, json.dumps(outcome.matrix), json.dumps(meta or {}),
    )

    await db.execute(
        "DELETE FROM aios.semantic_validation_dependency WHERE decision_type=$1 AND decision_key=$2",
        decision_type,
        decision_key,
    )
    for evidence_type, evidence_key in dependencies:
        await db.execute(
            """
            INSERT INTO aios.semantic_validation_dependency (
                decision_type, decision_key, evidence_type, evidence_key
            ) VALUES ($1,$2,$3,$4)
            ON CONFLICT DO NOTHING
            """,
            decision_type, decision_key, evidence_type, evidence_key,
        )


async def invalidate_for_evidence(
    db: Database,
    *,
    evidence_type: str,
    evidence_key: str,
    exclude_decision_type: Optional[str] = None,
    exclude_decision_key: Optional[str] = None,
) -> list[dict[str, str]]:
    rows = await db.fetch(
        """
        WITH RECURSIVE affected(decision_type, decision_key) AS (
            SELECT d.decision_type, d.decision_key
            FROM aios.semantic_validation_dependency d
            WHERE d.evidence_type=$1 AND d.evidence_key=$2
              AND NOT (
                  d.decision_type=COALESCE($3,'')
                  AND d.decision_key=COALESCE($4,'')
              )
            UNION
            SELECT child.decision_type, child.decision_key
            FROM affected a
            JOIN aios.semantic_validation_dependency child
              ON child.evidence_type='decision'
             AND child.evidence_key=(a.decision_type || ':' || a.decision_key)
        )
        UPDATE aios.semantic_validation_decision v
        SET status='stale', stale_at=COALESCE(v.stale_at, now())
        FROM affected a
        WHERE v.decision_type=a.decision_type
          AND v.decision_key=a.decision_key
          AND v.status <> 'stale'
        RETURNING v.decision_type, v.decision_key, v.subject_type, v.subject_key
        """,
        evidence_type,
        evidence_key,
        exclude_decision_type,
        exclude_decision_key,
    )
    return [dict(row) for row in rows]


async def apply_local_revalidation_invalidations(
    db: Database,
    affected: Iterable[dict[str, str]],
) -> None:
    for item in affected:
        dtype = item["decision_type"]
        skey = item["subject_key"]
        if dtype in {"semantic_owner", "world_assignment", "entity_referent"}:
            await db.execute(
                """
                UPDATE aios.semantic_topology_projection
                SET projected_at=NULL,
                    updated_at=now(),
                    meta=meta || jsonb_build_object('reproject_reason','semantic_validation_stale')
                WHERE claim_id::text=$1
                """,
                skey,
            )
        elif dtype == "reality_membership":
            await db.execute(
                """
                UPDATE aios.semantic_topology_projection stp
                SET projected_at=NULL,
                    updated_at=now(),
                    meta=stp.meta || jsonb_build_object('reproject_reason','reality_membership_stale')
                WHERE stp.claim_id IN (
                    SELECT o.claim_id
                    FROM aios.observation o
                    WHERE o.proposition_id::text=$1
                )
                """,
                skey,
            )
        elif dtype in {"proposition_relation", "event_identity"}:
            parts = skey.split(":", 1)
            if len(parts) == 2:
                await db.execute(
                    """
                    DELETE FROM aios.semantic_neighbor_relation
                    WHERE (proposition_id::text=$1 AND neighbor_proposition_id::text=$2)
                       OR (proposition_id::text=$2 AND neighbor_proposition_id::text=$1)
                    """,
                    parts[0], parts[1],
                )
        elif dtype == "epistemic_promotion":
            await db.execute(
                """
                UPDATE aios.world_proposition_assertion
                SET last_checked_at=NULL,
                    epistemic_status=CASE
                        WHEN source_kind='generated_fill' AND epistemic_status='corroborated'
                        THEN 'provisional'
                        ELSE epistemic_status
                    END,
                    updated_at=now()
                WHERE assertion_id::text=$1
                """,
                skey,
            )


async def notify_evidence_change(
    db: Database,
    *,
    evidence_type: str,
    evidence_key: str,
    exclude_decision_type: Optional[str] = None,
    exclude_decision_key: Optional[str] = None,
) -> list[dict[str, str]]:
    affected = await invalidate_for_evidence(
        db,
        evidence_type=evidence_type,
        evidence_key=evidence_key,
        exclude_decision_type=exclude_decision_type,
        exclude_decision_key=exclude_decision_key,
    )
    if affected:
        await apply_local_revalidation_invalidations(db, affected)
    return affected
