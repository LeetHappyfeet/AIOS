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
    claim_id: UUID
    proposition_id: UUID
    atom_id: UUID
    path: str
    scope_key: str
    outcome: str
    detail: dict[str, Any]


async def _record_receipt(
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
            claim_id, proposition_id, atom_id, path, scope_key,
            outcome, resolver_version, meta, reconciled_at, updated_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,now(),now())
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


async def _reconcile_world_atom(
    db: Database,
    *,
    world_id: UUID,
    atom_id: UUID,
) -> dict[str, Any]:
    """Collapse admitted world-scoped evidence into one current atom state.

    This deliberately reads only claims resolved to the world path. Character
    acquisitions are never consulted here, so /char belief cannot leak into
    /world merely because a character happens to believe the same proposition.
    Raw observations/propositions remain intact as provenance.
    """

    row = await db.fetchrow(
        """
        WITH evidence AS (
            SELECT
                p.proposition_id,
                p.polarity,
                o.observation_id,
                o.dag_node_id,
                LEAST(
                    0.999999,
                    GREATEST(
                        0.0,
                        COALESCE(pe.source_weight, 0.5)
                        * COALESCE(pe.confidence, o.extraction_confidence, 0.5)
                    )
                ) AS weight,
                COALESCE(o.source_key, 'unknown') || ':' ||
                    COALESCE(o.dag_node_id::text, o.observation_id::text) AS correlation_key,
                o.observed_at
            FROM aios.observation o
            JOIN aios.proposition p
              ON p.proposition_id=o.proposition_id
            JOIN aios.claim_context_resolution ccr
              ON ccr.claim_id=o.claim_id
            LEFT JOIN aios.proposition_evidence pe
              ON pe.proposition_id=p.proposition_id
             AND pe.observation_id=o.observation_id
             AND pe.evidence_role='support'
            LEFT JOIN aios.dag_node dn
              ON dn.node_id=o.dag_node_id
            LEFT JOIN aios.ingest_event ie
              ON ie.event_id=dn.event_id
            WHERE p.atom_id=$2
              AND ccr.world_id=$1
              AND ccr.epistemic_scope='world'
              AND (ie.event_id IS NULL OR ie.superseded_at IS NULL)
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
            SELECT proposition_id, polarity, dag_node_id
            FROM evidence
            ORDER BY weight DESC, observed_at DESC, proposition_id
            LIMIT 1
        )
        SELECT
            s.positive_support,
            s.negative_support,
            s.independent_evidence_count,
            (SELECT COUNT(*) FROM evidence) AS evidence_count,
            p.proposition_id AS preferred_proposition_id,
            p.polarity AS preferred_polarity,
            p.dag_node_id AS resolved_through_node_id
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
            preferred_proposition_id, evidence_count,
            independent_evidence_count, resolved_through_node_id,
            resolver_version, meta, resolved_at, updated_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,now(),now())
        ON CONFLICT (world_id, atom_id) DO UPDATE
        SET stance=EXCLUDED.stance,
            positive_support=EXCLUDED.positive_support,
            negative_support=EXCLUDED.negative_support,
            state_confidence=EXCLUDED.state_confidence,
            preferred_proposition_id=EXCLUDED.preferred_proposition_id,
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
        int(row["evidence_count"] or 0),
        int(row["independent_evidence_count"] or 0),
        row["resolved_through_node_id"],
        RECONCILIATION_STAGE_VERSION,
        json.dumps({"evidence_topology_preserved": True, "path": "world"}),
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
    """Reconcile one normalized claim into exactly one durable memory path.

    /char and /world remain deliberately separate. A character-scoped claim
    updates only the character belief materializer. A world-scoped claim updates
    only world memory. Source/unresolved scopes stay evidence-only until a later
    resolver has enough information to promote them safely.
    """

    row = await db.fetchrow(
        """
        SELECT
            o.claim_id,
            o.proposition_id,
            p.atom_id,
            ccr.epistemic_scope,
            ccr.world_id,
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
        }
        outcome = "character_belief_reconciled"
        path = "char"

    elif scope == "world" and row["world_id"] is not None:
        world_id = row["world_id"]
        detail = await _reconcile_world_atom(
            db,
            world_id=world_id,
            atom_id=atom_id,
        )
        scope_key = f"world:{world_id}"
        outcome = "world_memory_reconciled"
        path = "world"

    else:
        # Keep unresolved/source evidence queryable without pretending it is
        # either objective world state or a character's current belief.
        scope_key = (
            f"world:{row['world_id']}"
            if row["world_id"] is not None
            else f"claim:{claim_id}"
        )
        detail = {
            "epistemic_scope": scope,
            "reason": "scope_not_promotable",
            "context_confidence": float(row["context_confidence"] or 0.0),
        }
        outcome = "evidence_only"
        path = "evidence"

    await _record_receipt(
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
        proposition_id=proposition_id,
        atom_id=atom_id,
        path=path,
        scope_key=scope_key,
        outcome=outcome,
        detail=detail,
    )
