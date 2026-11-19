"""Opt-in review of stored parser goals; default operation is a dry run.

Only original, instance-owned cognitive source units can justify a repair.
Missing/recomputed units and operator-created goals are left for manual review.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from .goal_source_admission import ADMISSION_VERSION, NON_GOAL_REASONS, review_goal_source
from .goals import CharacterGoalService


async def revalidate_source_goals(db, instance_id, *, apply=False):
    findings = []
    async with db.atomic():
        rows = await db.fetch(
            """SELECT g.goal_id,g.meta,u.unit_id,u.meta AS unit_meta,c.node_id
               FROM aios.character_agent_goal g
               JOIN aios.message_cognitive_unit u ON u.unit_id::text=g.meta->>'source_unit_id'
               JOIN aios.message_cognitive_commit c ON c.commit_id=u.commit_id
                 AND c.instance_id=g.instance_id AND c.node_id=g.source_node_id
               WHERE g.instance_id=$1 AND g.status='active'
                 AND g.meta->>'source'='message_cognition' AND u.claim_kind='GOAL'
                 AND u.meta->>'character_owned'='true'
               FOR UPDATE OF g""", instance_id)
        for row in rows:
            meta = row["unit_meta"]
            meta = json.loads(meta) if isinstance(meta, str) else meta
            source, objective = meta.get("source_text"), meta.get("objective")
            if not source or not objective:
                continue
            span = meta.get("source_span")
            decision = review_goal_source(source_text=source, objective=objective,
                parse_reason=meta.get("parse_reason"), horizon=meta.get("horizon"),
                match_span=tuple(span) if isinstance(span, list) and len(span) == 2 else None)
            # Limit repairs to the new, well-defined exclusions. Other
            # ambiguities must not be mass-cancelled after a policy upgrade.
            if decision.reason not in NON_GOAL_REASONS | {
                "current_utterance_or_reaction", "incomplete_objective",
                "unresolved_goal_subject", "embedded_future_clause", "constraint_without_action",
                "transient_intention_horizon", "objective_contains_unresolved_reference",
                "unspecified_desired_outcome", "transient_scene_action",
                "immediate_intention", "figurative_objective",
            }:
                continue
            findings.append({"goal_id": str(row["goal_id"]), "objective": objective,
                             "reason": decision.reason, "policy": ADMISSION_VERSION,
                             "applied": apply})
            if apply:
                await CharacterGoalService(db).finish(
                    instance_id=instance_id, goal_id=row["goal_id"], status="cancelled",
                    resolution_kind="source_revalidation:" + ADMISSION_VERSION,
                    source_node_id=row["node_id"], evidence_ids=[str(row["unit_id"])],
                    refresh_scene=False)
    return findings


async def main():
    from aios_app.config import settings
    from aios_app.db import Database
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = Database(settings.db_dsn, max_size=1)
    await db.connect()
    try:
        print(json.dumps(await revalidate_source_goals(db, args.instance, apply=args.apply), indent=2))
    finally:
        await db.close()




async def repair_procedural_goals(db, *, limit=32):
    """Bounded upgrade of generated goals using their retained source evidence."""
    from .goal_source_admission import NON_GOAL_REASONS, non_goal_reason

    affected = set()
    async with db.atomic():
        rows = await db.fetch(
            """SELECT g.goal_id,g.instance_id,g.goal_text,g.source_node_id,g.meta,
                      u.meta AS unit_meta,u.unit_id
               FROM aios.character_agent_goal g
               LEFT JOIN aios.message_cognitive_unit u
                 ON u.unit_id::text=g.meta->>'source_unit_id'
                 AND u.claim_kind='GOAL' AND u.meta->>'character_owned'='true'
                 AND EXISTS (SELECT 1 FROM aios.message_cognitive_commit c
                             WHERE c.commit_id=u.commit_id AND c.instance_id=g.instance_id
                               AND c.node_id=g.source_node_id)
               WHERE g.status IN ('active','scheduled')
                 AND (g.meta->>'source'='message_cognition'
                      OR g.meta->>'created_by'='legacy_runtime_goal_import')
                 AND g.meta->>'procedural_review_version' IS DISTINCT FROM $1
               ORDER BY g.created_at,g.goal_id
               LIMIT $2 FOR UPDATE OF g SKIP LOCKED""",
            ADMISSION_VERSION, max(1, min(int(limit), 128)),
        )
        for row in rows:
            meta = CharacterGoalService._json_object(row["meta"])
            unit = CharacterGoalService._json_object(row["unit_meta"])
            objective = unit.get("objective") or meta.get("objective") or row["goal_text"]
            reason = non_goal_reason(objective)
            if unit.get("source_text") and unit.get("objective"):
                span = unit.get("source_span")
                review = review_goal_source(
                    source_text=unit["source_text"], objective=unit["objective"],
                    parse_reason=unit.get("parse_reason"), horizon=unit.get("horizon"),
                    match_span=tuple(span) if isinstance(span, list) and len(span) == 2 else None,
                )
                # Reapply the existing utterance/constraint exclusions only
                # where the original character-owned source unit is available.
                if review.reason in NON_GOAL_REASONS | {
                    "current_utterance_or_reaction", "objective_contains_unresolved_reference",
                    "embedded_future_clause", "constraint_without_action",
                }:
                    reason = review.reason
            if reason:
                await CharacterGoalService(db).finish(
                    instance_id=row["instance_id"], goal_id=row["goal_id"], status="cancelled",
                    resolution_kind="source_revalidation:" + ADMISSION_VERSION + ":" + reason,
                    source_node_id=row["source_node_id"],
                    evidence_ids=[str(row["unit_id"])] if row["unit_id"] else [],
                    refresh_scene=False,
                )
                affected.add(row["instance_id"])
            await db.execute(
                """UPDATE aios.character_agent_goal SET meta=meta || $2::jsonb
                   WHERE goal_id=$1""",
                row["goal_id"], json.dumps({"procedural_review_version": ADMISSION_VERSION}),
            )
    for instance_id in affected:
        await CharacterGoalService(db)._invalidate(instance_id)
        await CharacterGoalService(db)._refresh_scene(instance_id)
    return len(rows)


if __name__ == "__main__":
    asyncio.run(main())
