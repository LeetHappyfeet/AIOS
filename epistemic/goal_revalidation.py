"""Opt-in review of stored parser goals; default operation is a dry run.

Only original, instance-owned cognitive source units can justify a repair.
Missing/recomputed units and operator-created goals are left for manual review.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from .goal_source_admission import ADMISSION_VERSION, review_goal_source
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
            if decision.reason not in {
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


if __name__ == "__main__":
    asyncio.run(main())
