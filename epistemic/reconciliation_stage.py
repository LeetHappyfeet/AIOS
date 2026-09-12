from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.belief_reconciliation import reconcile_character_belief_atom


RECONCILIATION_STAGE_VERSION = "memory-reconciliation-v1"


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


async def _reconcile_world_atom(
    db: Database,
    *,
    world_id: UUID,
    atom_id: UUID,
) -> dict[str, Any]:
    """Collapse explicit /world assertions into one current semantic state.

    world_proposition_assertion is the authority boundary. Raw narrative/source
    observations and character acquisitions are intentionally absent from this
    query, so neither can silently become objective world memory.
    """

    row = await db.fetchrow(
        """
        WITH evidence AS (
            SELECT
                a.assertion_id,
                a.proposition_id,
                p.polarity,
                LEAST(0.999999, GREATEST(0.0, COALESCE(a.confidence, 0.0))) AS weight,
                COALESCE(a.source_kind, 'unknown') || ':' || a.assertion_id::text
                    AS correlation_key,
                a.generated_at_node_id,
                a.updated_at
            FROM aios.world_proposition_assertion a
            JOIN aios.proposition p
              ON p.proposition_id=a.proposition_id
            WHERE a.world_id=$1
              AND p.atom_id=$2
              AND a.epistemic_status NOT IN ('rejected','superseded')
        ),
        correlated AS (
            SELECT polarity, correlation_key, MAX(weight) AS weight
            FROM evidence
            GROUP BY polarity, correlation_key
        ),
        support AS (
            SELECT
                COALESCE(
                    1.0 - exp(SUM(ln(1.0 - weight)) FILTER (WHERE polarity=1)),
                    0.0
                ) AS positive_support,
                COALESCE(
                    1.0 - exp(SUM(ln(1.0 - weight)) FILTER (WHERE polarity=-1)),
                    0.0
                ) AS negative_support,
                COUNT(*) AS independent_evidence_count
            FROM correlated
        ),
        preferred AS (
            SELECT assertion_id, proposition_id, polarity, generated_at_node_id
            FROM evidence
            ORDER BY weight DESC, updated_at DESC, assertion_id
            LIMIT 1
        )
        SELECT
            s.positive_support,
            s.negative_support,
            s.independent_evidence_count,
            (SELECT COUNT(*) FROM evidence) AS evidence_count,
            p.assertion_id AS preferred_assertion_id,
            p.proposition_id AS preferred_proposition_id,
            p.polarity AS preferred_polarity,
            p.generated_at_node_id AS resolved_through_node_id
        FROM support s
        LEFT JOIN preferred p ON true
        """,
        world_id,
        atom_id,
    )

    if not row or int(row["evidence_count"] or 0) == 0:
        await db.execute(
            "DELETE FROM aios.world_memory_state WHERE world_id=$1 AND atom_id=$2",
            world_id,
            atom_id,
        )
        return {"status": "empty"}

    positive = float(row["positive_support"] or 0.0)
    negative = float(row["negative_support"] or 0.0)
    margin = positive - negative
    if positive >= 0.60 and margin >= 0.15:
        stance = "positive"
    elif negative >= 0.60 and -margin >= 0.15:
        stance = "negative"
    else:
        stance = "unresolved"

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
        abs(margin),
        row["preferred_proposition_id"],
        row["preferred_assertion_id"],
        int(row["evidence_count"] or 0),
        int(row["independent_evidence_count"] or 0),
        row["resolved_through_node_id"],
        RECONCILIATION_STAGE_VERSION,
        json.dumps({
            "path": "world",
            "authority_boundary": "world_proposition_assertion",
            "evidence_topology_preserved": True,
        }),
    )

    return {
        "status": "materialized",
        "stance": stance,
        "positive_support": positive,
        "negative_support": negative,
        "evidence_count": int(row["evidence_count"] or 0),
        "independent_evidence_count": int(row["independent_evidence_count"] or 0),
    }


async def reconcile_claim_memory(db: Database, *, claim_id: UUID) -> ReconciliationResult:
    """Route one normalized claim without allowing implicit /world promotion.

    Character-scoped claims may update /char belief because they have an exact
    character instance. Narrative, speaker, source, and unresolved claims remain
    evidence-only. Objective /world state is reconciled separately from explicit
    world_proposition_assertion rows via reconcile_world_assertion().
    """

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
        JOIN aios.proposition p
          ON p.proposition_id=o.proposition_id
        JOIN aios.claim_context_resolution ccr
          ON ccr.claim_id=o.claim_id
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
        await reconcile_character_belief_atom(
            db,
            instance_id=instance_id,
            atom_id=atom_id,
        )
        scope_key = f"char:{row['origin_character_id'] or instance_id}"
        detail = {
            "instance_id": str(instance_id),
            "context_confidence": float(row["context_confidence"] or 0.0),
            "world_promotion": False,
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
