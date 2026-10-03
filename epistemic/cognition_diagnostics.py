"""Read-only, causal cognition-to-managed-goal diagnostic for one instance.

This command does not create goals, replay older interpreter receipts, mutate
a HUD cursor, or infer the intended outcome from participation percentages.
"""
from __future__ import annotations

import json
from uuid import UUID


async def diagnose_goal_pipeline(db, *, instance_id: UUID,
                                 experiment_id: UUID | None = None) -> dict:
    # Current-head ancestry, not all nodes sharing a timeline/event range.
    source = await db.fetch(
        """WITH RECURSIVE chain AS (
             SELECT dn.node_id,dn.timeline_id,dn.event_id,dn.speaker_id,
                    ARRAY[dn.node_id]::uuid[] AS visited,0 AS depth
             FROM aios.character_runtime_state rs
             JOIN aios.dag_node dn
               ON dn.node_id=rs.source_head_node_id
              AND dn.timeline_id=rs.source_timeline_id
             WHERE rs.instance_id=$1
             UNION ALL
             SELECT p.node_id,p.timeline_id,p.event_id,p.speaker_id,
                    ch.visited||p.node_id,ch.depth+1
             FROM chain ch
             JOIN aios.dag_edge edge
               ON edge.timeline_id=ch.timeline_id
              AND edge.child_node_id=ch.node_id
             JOIN aios.dag_node p
               ON p.node_id=edge.parent_node_id
              AND p.timeline_id=edge.timeline_id
             WHERE ch.depth<4096 AND NOT p.node_id=ANY(ch.visited)
           )
           SELECT ch.event_id,ch.node_id,ch.speaker_id,
                  c.interpreter_version,c.summary,c.enrichment_completed_at,
                  COUNT(u.unit_id) AS units,
                  COUNT(u.unit_id) FILTER (WHERE u.claim_kind='GOAL') AS goal_units,
                  COUNT(u.unit_id) FILTER (
                    WHERE u.claim_kind='GOAL' AND u.polarity>0
                      AND COALESCE(u.meta->>'character_owned','false')='true'
                  ) AS positive_owned_goal_units,
                  COUNT(u.unit_id) FILTER (
                    WHERE u.claim_kind='GOAL' AND
                      u.meta ? 'historical_goal_reconciliation'
                  ) AS reviewed_goal_units
           FROM chain ch
           JOIN aios.dag_node dn ON dn.node_id=ch.node_id
           LEFT JOIN aios.message_cognitive_commit c
             ON c.node_id=ch.node_id AND c.instance_id=$1
           LEFT JOIN aios.message_cognitive_unit u ON u.commit_id=c.commit_id
           WHERE btrim(COALESCE(dn.message_text,''))<>''
           GROUP BY ch.event_id,ch.node_id,ch.speaker_id,c.interpreter_version,
                    c.summary,c.enrichment_completed_at
           ORDER BY ch.event_id,ch.node_id
           LIMIT 256""", instance_id,
    )
    managed = await db.fetch(
        """SELECT goal_id,goal_text,status,source_node_id,
                  meta->>'semantic_topic_key' AS topic_key,
                  meta->>'source' AS source_kind,created_at,updated_at
           FROM aios.character_agent_goal
           WHERE instance_id=$1 ORDER BY created_at,goal_id LIMIT 128""",
        instance_id,
    )
    readiness = await db.fetchrow(
        """SELECT source_head_node_id,cognitive_ready_node_id,
                  cognitive_ready_event_id,retrieval_ready_node_id,
                  retrieval_ready_event_id
           FROM aios.character_hud_readiness WHERE instance_id=$1""",
        instance_id,
    )
    samples = []
    missing = positive = deferred = pending = zero = 0
    for row in source:
        record = dict(row)
        summary = record.get("summary") or {}
        if isinstance(summary, str):
            summary = json.loads(summary)
        if not isinstance(summary, dict):
            summary = {}
        if not record["interpreter_version"]:
            missing += 1
        if not int(record["units"] or 0) and record["interpreter_version"]:
            zero += 1
        positive += int(record["positive_owned_goal_units"] or 0)
        if summary.get("enrichment_pending"):
            pending += 1
        if summary.get("goal_projection_deferred"):
            deferred += 1
        samples.append({
            "event_id": record["event_id"], "node_id": str(record["node_id"]),
            "speaker_id": record["speaker_id"],
            "interpreter_version": record["interpreter_version"],
            "unit_count": int(record["units"] or 0),
            "positive_owned_goal_units": int(record["positive_owned_goal_units"] or 0),
            "candidate_outcome": summary.get("candidate_outcome"),
            "candidate_rejections": summary.get("candidate_rejections", []),
            "enrichment_pending": bool(summary.get("enrichment_pending")),
            "enrichment_deferred": bool(summary.get("enrichment_deferred")),
            "goal_projection_deferred": bool(summary.get("goal_projection_deferred")),
            "enrichment_rejections": summary.get("enrichment_rejections", []),
        })
    result = {
        "instance_id": str(instance_id),
        "source_nodes_sampled": len(samples), "source_nodes_without_commit": missing,
        "committed_zero_unit_nodes": zero, "positive_owned_goal_units": positive,
        "nodes_pending_enrichment": pending, "nodes_with_deferred_goals": deferred,
        "managed_goals_total": len(managed),
        "managed_goals_active": sum(1 for goal in managed if goal["status"] == "active"),
        "managed_goal_rows": [dict(row) for row in managed],
        "hud_readiness": dict(readiness) if readiness else None,
        "cognition_samples": samples,
        "notes": [
            "Counts reflect sampled ancestors of the current source head only.",
            "A missing goal in participation may reflect admission timing; historical state is not inferred.",
            "Existing older-version commits are not automatically reinterpreted.",
        ],
    }
    if missing:
        result["first_observed_blocker"] = "source_nodes_without_cognition_receipt"
    elif pending:
        result["first_observed_blocker"] = "bounded_enrichment_incomplete"
    elif deferred:
        result["first_observed_blocker"] = "historical_goal_projection_deferred"
    elif not positive:
        result["first_observed_blocker"] = "no_positive_owned_goal_units"
    elif not any(g["status"] == "active" for g in managed):
        result["first_observed_blocker"] = "goal_admission_or_terminal_lifecycle"
    else:
        result["first_observed_blocker"] = None
    if experiment_id is not None:
        snap = await db.fetchrow(
            """SELECT count(*) AS evaluated,
               count(*) FILTER (
                 WHERE jsonb_array_length(context_snapshot->'goals')>0
               ) AS evaluations_with_goals,
               min(evaluated_at) AS first_evaluation,
               max(evaluated_at) AS last_evaluation
               FROM aios.character_participation_evaluation
               WHERE instance_id=$1 AND experiment_id=$2""",
            instance_id, experiment_id,
        )
        result["participation_at_evaluation"] = dict(snap) if snap else None
    return result


async def _cli() -> None:
    import argparse
    from aios_app.config import settings
    from aios_app.db import Database
    parser = argparse.ArgumentParser(description="Read-only cognition-to-goal stage diagnostics")
    parser.add_argument("--instance-id", required=True, type=UUID)
    parser.add_argument("--experiment-id", type=UUID)
    args = parser.parse_args()
    db = Database(settings.db_dsn, min_size=1, max_size=2)
    await db.connect()
    try:
        result = await diagnose_goal_pipeline(
            db, instance_id=args.instance_id, experiment_id=args.experiment_id,
        )
        print(json.dumps(result, default=str, indent=2))
    finally:
        await db.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_cli())
