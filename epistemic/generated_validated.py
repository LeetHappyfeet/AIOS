from __future__ import annotations

import json

from aios_app.db import Database
from aios_app.epistemic.promotion_validator import validate_generated_promotion
from aios_app.epistemic.hypothesis_validation import notify_evidence_change


async def resolve_generated_facts_validated(db: Database, *, limit: int = 100) -> int:
    rows = await db.fetch(
        """
        SELECT a.assertion_id, a.world_id, a.proposition_id, a.confidence,
               a.last_checked_at, p.topic_key
        FROM aios.world_proposition_assertion a
        JOIN aios.proposition p ON p.proposition_id=a.proposition_id
        WHERE a.source_kind='generated_fill'
          AND a.epistemic_status IN ('provisional','corroborated')
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

    changed = 0
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
                    CASE WHEN pc.proposition_a_id=$2 THEN pc.proposition_b_id
                         ELSE pc.proposition_a_id END AS proposition_id,
                    pc.conflict_type,
                    pc.strength
                FROM aios.proposition_conflict pc
                WHERE pc.proposition_a_id=$2 OR pc.proposition_b_id=$2
            ), scored AS (
                SELECT c.proposition_id, c.conflict_type, c.strength,
                    GREATEST(
                        COALESCE((
                            SELECT max(COALESCE(NULLIF(o.extraction_confidence,0),0.5))
                            FROM aios.observation o
                            JOIN aios.timeline t ON t.timeline_id=o.timeline_id
                            WHERE t.world_id=$1 AND o.proposition_id=c.proposition_id
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

        conflict_strength = float(conflict["strength"] or 0.0) if conflict else 0.0
        conflict_confidence = float(conflict["observed_confidence"] or 0.0) if conflict else 0.0
        competing_id = conflict["competing_proposition_id"] if conflict else None
        action, verification = await validate_generated_promotion(
            db,
            assertion_id=generated["assertion_id"],
            world_id=generated["world_id"],
            proposition_id=generated["proposition_id"],
            support_confidence=support_conf,
            conflict_strength=conflict_strength,
            conflict_observed_confidence=conflict_confidence,
            competing_proposition_id=competing_id,
        )

        if action == "SUPERSEDE" and conflict:
            from aios_app.epistemic.generated import assert_observed_proposition_in_world
            competing_assertion_id = await assert_observed_proposition_in_world(
                db,
                world_id=generated["world_id"],
                proposition_id=competing_id,
                confidence=conflict_confidence,
                reason=f"matrix supersedes generated fill: {conflict['conflict_type']}",
            )
            await db.execute(
                """
                UPDATE aios.world_proposition_assertion
                SET epistemic_status='superseded',
                    superseded_by_assertion_id=$2,
                    last_checked_at=now(), updated_at=now(),
                    meta=meta || $3::jsonb
                WHERE assertion_id=$1
                """,
                generated["assertion_id"],
                competing_assertion_id,
                json.dumps({"resolution": "adversarial_supersession", "verification": verification}),
            )
            changed += 1
        elif action == "CORROBORATE" and support_conf > 0:
            await db.execute(
                """
                UPDATE aios.world_proposition_assertion
                SET epistemic_status='corroborated',
                    confidence=GREATEST(confidence,$2),
                    last_checked_at=now(), updated_at=now(),
                    meta=meta || $3::jsonb
                WHERE assertion_id=$1
                """,
                generated["assertion_id"],
                support_conf,
                json.dumps({"resolution": "adversarial_corroboration", "verification": verification}),
            )
            changed += 1
        else:
            await db.execute(
                """
                UPDATE aios.world_proposition_assertion
                SET epistemic_status='provisional',
                    last_checked_at=now(), updated_at=now(),
                    meta=meta || $2::jsonb
                WHERE assertion_id=$1
                """,
                generated["assertion_id"],
                json.dumps({"resolution": "remain_provisional", "verification": verification}),
            )

        # Publish the materialized world-state change to all OTHER dependent
        # decisions. Do not invalidate the promotion decision that just created
        # this materialization; later independent evidence may invalidate it.
        await notify_evidence_change(
            db,
            evidence_type="world_proposition",
            evidence_key=f"{generated['world_id']}:{generated['proposition_id']}",
            exclude_decision_type="epistemic_promotion",
            exclude_decision_key=str(generated["assertion_id"]),
        )

    return changed
