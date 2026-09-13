from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aios_app.db import Database


RESOLVER_VERSION = "character-belief-v1"
DEFAULT_ACCEPT_SUPPORT = 0.60
DEFAULT_DECISION_MARGIN = 0.15


@dataclass(frozen=True)
class BeliefDecision:
    stance: str
    positive_support: float
    negative_support: float
    confidence: float


def choose_belief_stance(
    positive_support: float,
    negative_support: float,
    *,
    accept_support: float = DEFAULT_ACCEPT_SUPPORT,
    decision_margin: float = DEFAULT_DECISION_MARGIN,
) -> BeliefDecision:
    """Apply the deterministic stance policy used by the SQL materializer.

    This helper is intentionally small: evidence weighting, correlation and
    branch visibility are database concerns. It exists so the policy boundary
    is easy to inspect and unit-test without pretending that a single evidence
    confidence value is itself a belief.
    """

    positive = max(0.0, min(1.0, float(positive_support)))
    negative = max(0.0, min(1.0, float(negative_support)))
    accept = max(0.0, min(1.0, float(accept_support)))
    margin = max(0.0, min(1.0, float(decision_margin)))

    if positive >= accept and positive - negative >= margin:
        stance = "positive"
    elif negative >= accept and negative - positive >= margin:
        stance = "negative"
    else:
        stance = "unresolved"

    return BeliefDecision(
        stance=stance,
        positive_support=positive,
        negative_support=negative,
        confidence=abs(positive - negative),
    )


async def reconcile_character_belief_atom(
    db: Database,
    *,
    instance_id: UUID,
    atom_id: UUID,
) -> None:
    """Force reconciliation for one character-instance semantic atom."""

    await db.execute(
        "SELECT aios.reconcile_character_belief_atom($1,$2)",
        instance_id,
        atom_id,
    )


async def reconcile_instance_beliefs(
    db: Database,
    *,
    instance_id: UUID,
) -> int:
    """Reconcile every semantic atom visible through an instance's lineage."""

    rows = await db.fetch(
        """
        WITH RECURSIVE lineage AS (
            SELECT ci.instance_id, ci.parent_instance_id
            FROM aios.character_instance ci
            WHERE ci.instance_id=$1

            UNION ALL

            SELECT parent.instance_id, parent.parent_instance_id
            FROM lineage
            JOIN aios.character_instance parent
              ON parent.instance_id=lineage.parent_instance_id
        )
        SELECT DISTINCT p.atom_id
        FROM lineage
        JOIN aios.character_proposition_knowledge cpk
          ON cpk.instance_id=lineage.instance_id
        JOIN aios.proposition p
          ON p.proposition_id=cpk.proposition_id
        WHERE p.atom_id IS NOT NULL
        ORDER BY p.atom_id
        """,
        instance_id,
    )

    for row in rows:
        await reconcile_character_belief_atom(
            db,
            instance_id=instance_id,
            atom_id=row["atom_id"],
        )
    return len(rows)


async def get_character_belief_states(
    db: Database,
    *,
    instance_id: UUID,
    include_unresolved: bool = True,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Return current materialized belief state, never raw acquisition evidence."""

    rows = await db.fetch(
        """
        SELECT
            bs.*,
            a.subject_norm,
            a.predicate_norm,
            a.object_norm,
            p.canonical_text AS preferred_text,
            p.polarity AS preferred_polarity
        FROM aios.character_belief_state bs
        JOIN aios.semantic_atom a ON a.atom_id=bs.atom_id
        LEFT JOIN aios.proposition p
          ON p.proposition_id=bs.preferred_proposition_id
        WHERE bs.instance_id=$1
          AND ($2::boolean OR bs.stance <> 'unresolved')
        ORDER BY bs.belief_confidence DESC, bs.updated_at DESC, bs.atom_id
        LIMIT $3
        """,
        instance_id,
        include_unresolved,
        max(1, min(int(limit), 1000)),
    )
    return [dict(row) for row in rows]
