from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from aios_app.db import Database
from .normalizer import ensure_proposition


async def create_generated_fact(
    db: Database,
    *,
    world_id: UUID,
    subject: Optional[str],
    predicate: Optional[str],
    object_value: Optional[str],
    raw_text: str,
    confidence: float = 0.35,
    generated_at_node_id: Optional[UUID] = None,
    reason: Optional[str] = None,
    meta: Optional[dict] = None,
) -> dict:
    """Create a gap-fill proposition without silently declaring it world truth."""
    world = await db.fetchrow(
        "SELECT world_id FROM aios.world WHERE world_id=$1",
        world_id,
    )
    if not world:
        raise ValueError(f"unknown world {world_id}")

    proposition_id = await ensure_proposition(
        db,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        raw_text=raw_text,
        modality="generated",
        meta={"generated_fill": True, **(meta or {})},
    )

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.world_proposition_assertion (
            world_id, proposition_id, epistemic_status, source_kind,
            confidence, generated_at_node_id, reason, meta
        )
        VALUES ($1,$2,'provisional','generated_fill',$3,$4,$5,$6::jsonb)
        ON CONFLICT (world_id, proposition_id, source_kind) DO UPDATE
        SET confidence=GREATEST(aios.world_proposition_assertion.confidence,
                                EXCLUDED.confidence),
            reason=COALESCE(EXCLUDED.reason, aios.world_proposition_assertion.reason),
            updated_at=now(),
            last_checked_at=NULL,
            meta=aios.world_proposition_assertion.meta || EXCLUDED.meta
        RETURNING assertion_id, proposition_id, epistemic_status,
                  confidence, source_kind
        """,
        world_id,
        proposition_id,
        max(0.0, min(1.0, confidence)),
        generated_at_node_id,
        reason,
        json.dumps(meta or {}),
    )
    return dict(row)


async def assert_observed_proposition_in_world(
    db: Database,
    *,
    world_id: UUID,
    proposition_id: UUID,
    confidence: float,
    reason: str,
) -> UUID:
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.world_proposition_assertion (
            world_id, proposition_id, epistemic_status, source_kind,
            confidence, reason, meta
        )
        VALUES ($1,$2,'tentative','observed',$3,$4,$5::jsonb)
        ON CONFLICT (world_id, proposition_id, source_kind) DO UPDATE
        SET confidence=GREATEST(aios.world_proposition_assertion.confidence,
                                EXCLUDED.confidence),
            updated_at=now()
        RETURNING assertion_id
        """,
        world_id,
        proposition_id,
        max(0.0, min(1.0, confidence)),
        reason,
        json.dumps({"policy": "generated-resolution-v1"}),
    )
    return row["assertion_id"]


async def resolve_generated_facts_once(db: Database, *, limit: int = 100) -> int:
    """
    Reconcile provisional generated facts against later concrete evidence.

    Evidence can arrive through runtime observations or through an explicit
    observed proposition imported into the world during RP/bootstrap.
    """
    rows = await db.fetch(
        """
        SELECT a.assertion_id, a.world_id, a.proposition_id, a.confidence,
               a.last_checked_at, p.topic_key
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        WHERE a.source_kind='generated_fill'
          AND a.epistemic_status='provisional'
          AND (
              a.last_checked_at IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM aios.observation o
                  JOIN aios.timeline t ON t.timeline_id=o.timeline_id
                  JOIN aios.proposition op ON op.proposition_id=o.proposition_id
                  WHERE t.world_id=a.world_id
                    AND op.topic_key=p.topic_key
                    AND o.observed_at > a.last_checked_at
              )
              OR EXISTS (
                  SELECT 1
                  FROM aios.world_proposition_assertion wa
                  JOIN aios.proposition wp ON wp.proposition_id=wa.proposition_id
                  WHERE wa.world_id=a.world_id
                    AND wa.source_kind='observed'
                    AND wa.epistemic_status NOT IN ('rejected','superseded')
                    AND wp.topic_key=p.topic_key
                    AND wa.updated_at > a.last_checked_at
              )
          )
        ORDER BY a.created_at
        LIMIT $1
        """,
        limit,
    )

    resolved = 0
    for generated in rows:
        support = await db.fetchrow(
            """
            SELECT GREATEST(
                COALESCE((
                    SELECT max(COALESCE(NULLIF(o.extraction_confidence,0),0.5))
                    FROM aios.observation o
                    JOIN aios.timeline t ON t.timeline_id=o.timeline_id
                    WHERE t.world_id=$1 AND o.proposition_id=$2
                ),0),
                COALESCE((
                    SELECT max(a.confidence)
                    FROM aios.world_proposition_assertion a
                    WHERE a.world_id=$1
                      AND a.proposition_id=$2
                      AND a.source_kind='observed'
                      AND a.epistemic_status NOT IN ('rejected','superseded')
                ),0)
            ) AS confidence
            """,
            generated["world_id"],
            generated["proposition_id"],
        )
        support_conf = float(support["confidence"] or 0.0)

        conflict = await db.fetchrow(
            """
            WITH competing AS (
                SELECT
                    CASE
                        WHEN pc.proposition_a_id=$2 THEN pc.proposition_b_id
                        ELSE pc.proposition_a_id
                    END AS proposition_id,
                    pc.conflict_type,
                    pc.strength
                FROM aios.proposition_conflict pc
                WHERE pc.proposition_a_id=$2 OR pc.proposition_b_id=$2
            ),
            scored AS (
                SELECT
                    c.proposition_id,
                    c.conflict_type,
                    c.strength,
                    GREATEST(
                        COALESCE((
                            SELECT max(COALESCE(NULLIF(o.extraction_confidence,0),0.5))
                            FROM aios.observation o
                            JOIN aios.timeline t ON t.timeline_id=o.timeline_id
                            WHERE t.world_id=$1
                              AND o.proposition_id=c.proposition_id
                        ),0),
                        COALESCE((
                            SELECT max(a.confidence)
                            FROM aios.world_proposition_assertion a
                            WHERE a.world_id=$1
                              AND a.proposition_id=c.proposition_id
                              AND a.source_kind='observed'
                              AND a.epistemic_status NOT IN ('rejected','superseded')
                        ),0)
                    ) AS observed_confidence
                FROM competing c
            )
            SELECT proposition_id AS competing_proposition_id,
                   conflict_type, strength, observed_confidence
            FROM scored
            WHERE observed_confidence > 0
            ORDER BY strength * observed_confidence DESC
            LIMIT 1
            """,
            generated["world_id"],
            generated["proposition_id"],
        )

        if conflict:
            competing_assertion_id = await assert_observed_proposition_in_world(
                db,
                world_id=generated["world_id"],
                proposition_id=conflict["competing_proposition_id"],
                confidence=float(conflict["observed_confidence"]),
                reason=f"supersedes generated fill: {conflict['conflict_type']}",
            )
            await db.execute(
                """
                UPDATE aios.world_proposition_assertion
                SET epistemic_status='superseded',
                    superseded_by_assertion_id=$2,
                    last_checked_at=now(),
                    updated_at=now(),
                    meta=meta || $3::jsonb
                WHERE assertion_id=$1
                """,
                generated["assertion_id"],
                competing_assertion_id,
                json.dumps({
                    "resolution": "observed_conflict",
                    "conflict_type": conflict["conflict_type"],
                }),
            )
            resolved += 1
            continue

        if support_conf > 0.0:
            await db.execute(
                """
                UPDATE aios.world_proposition_assertion
                SET epistemic_status='corroborated',
                    confidence=GREATEST(confidence,$2),
                    last_checked_at=now(),
                    updated_at=now(),
                    meta=meta || $3::jsonb
                WHERE assertion_id=$1
                """,
                generated["assertion_id"],
                support_conf,
                json.dumps({"resolution": "later_observation_support"}),
            )
            resolved += 1
            continue

        await db.execute(
            """
            UPDATE aios.world_proposition_assertion
            SET last_checked_at=now(), updated_at=now()
            WHERE assertion_id=$1
            """,
            generated["assertion_id"],
        )

    return resolved
