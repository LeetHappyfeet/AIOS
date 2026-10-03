"""Bounded, source-timeline-scoped recovery of missing message cognition.

The live HUD processes its current head immediately. Catch-up separately
revisits missing earlier nodes in event order. A zero-unit cognition commit is
a successful receipt, not a gap. Historical evidence remains historical:
the positive-goal projection for recovered old nodes is deferred to the
chronological goal-reconciliation pass.
"""
from __future__ import annotations

import logging
from uuid import UUID

from aios_app.epistemic.message_cognition import commit_message_cognition

logger = logging.getLogger("aios.epistemic.cognition_catchup")
BATCH_LIMIT = 32


async def missing_cognition_nodes(db, *, instance_id: UUID, limit: int = BATCH_LIMIT):
    if not 1 <= limit <= BATCH_LIMIT:
        raise ValueError("Catchup batch limit must be 1..32")
    return await db.fetch(
        """WITH RECURSIVE source_chain AS (
             SELECT head.node_id,head.timeline_id,head.event_id,
                    ARRAY[head.node_id]::uuid[] AS visited,0 AS depth,
                    head.node_id AS root_node_id
             FROM aios.character_runtime_state rs
             JOIN aios.dag_node head
               ON head.node_id=rs.source_head_node_id
              AND head.timeline_id=rs.source_timeline_id
             WHERE rs.instance_id=$1
             UNION ALL
             SELECT parent.node_id,parent.timeline_id,parent.event_id,
                    chain.visited || parent.node_id,chain.depth+1,
                    chain.root_node_id
             FROM source_chain chain
             JOIN aios.dag_edge edge
               ON edge.timeline_id=chain.timeline_id
              AND edge.child_node_id=chain.node_id
             JOIN aios.dag_node parent
               ON parent.node_id=edge.parent_node_id
              AND parent.timeline_id=edge.timeline_id
             WHERE chain.depth<4096
               AND NOT parent.node_id=ANY(chain.visited)
           )
           SELECT DISTINCT dn.node_id,dn.event_id,chain.root_node_id
           FROM source_chain chain
           JOIN aios.dag_node dn ON dn.node_id=chain.node_id
           JOIN aios.character_runtime_state rs
             ON rs.instance_id=$1 AND rs.source_timeline_id=dn.timeline_id
           WHERE dn.message_text IS NOT NULL
             AND btrim(dn.message_text)<>''
             AND NOT EXISTS (
               SELECT 1 FROM aios.message_cognitive_commit c
               WHERE c.instance_id=rs.instance_id AND c.node_id=dn.node_id
             )
             AND (
               NOT EXISTS (
                 SELECT 1 FROM aios.conversation_participant cp
                 WHERE cp.timeline_id=rs.source_timeline_id
                   AND cp.character_instance_id=rs.instance_id
               )
               OR EXISTS (
                 SELECT 1 FROM aios.conversation_participant cp
                 JOIN aios.message_participant mp
                   ON mp.participant_id=cp.participant_id
                 WHERE cp.timeline_id=rs.source_timeline_id
                   AND cp.character_instance_id=rs.instance_id
                   AND mp.node_id=dn.node_id AND mp.perceived
               )
             )
           ORDER BY dn.event_id ASC, dn.node_id ASC
           LIMIT $2""",
        instance_id, limit,
    )


async def deferred_enrichment_nodes(db, *, instance_id: UUID, limit: int = 4):
    """Only current-branch historical messages with unfinished inference."""
    if not 1 <= limit <= 4:
        raise ValueError("Historical enrichment batch limit must be 1..4")
    return await db.fetch(
        """WITH RECURSIVE source_chain AS (
             SELECT head.node_id,head.timeline_id,
                    ARRAY[head.node_id]::uuid[] AS visited,0 AS depth
             FROM aios.character_runtime_state rs
             JOIN aios.dag_node head
               ON head.node_id=rs.source_head_node_id
              AND head.timeline_id=rs.source_timeline_id
             WHERE rs.instance_id=$1
             UNION ALL
             SELECT parent.node_id,parent.timeline_id,
                    chain.visited||parent.node_id,chain.depth+1
             FROM source_chain chain
             JOIN aios.dag_edge de
               ON de.timeline_id=chain.timeline_id AND de.child_node_id=chain.node_id
             JOIN aios.dag_node parent
               ON parent.node_id=de.parent_node_id AND parent.timeline_id=de.timeline_id
             WHERE chain.depth<4096 AND NOT parent.node_id=ANY(chain.visited)
           )
           SELECT c.node_id,c.event_id
           FROM aios.message_cognitive_commit c
           JOIN source_chain chain ON chain.node_id=c.node_id
           WHERE c.instance_id=$1
             AND c.summary->>'historical_catchup'='true'
             AND c.summary->>'enrichment_deferred'='true'
             AND c.summary->>'enrichment_pending'='true'
           ORDER BY c.event_id,c.node_id LIMIT $2""",
        instance_id, limit,
    )


async def recover_missing_cognition(db, *, instance_id: UUID,
                                    limit: int = BATCH_LIMIT) -> dict:
    """A single bounded pass. Repeated passes resume from commit receipts."""
    nodes = await missing_cognition_nodes(db, instance_id=instance_id, limit=limit)
    committed = 0
    failed = 0
    for node in nodes:
        try:
            if await commit_message_cognition(
                db, instance_id=instance_id, node_id=node["node_id"],
                expected_head_node_id=node.get("root_node_id"),
            ):
                committed += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            logger.exception("Cognition recovery failed instance=%s node=%s",
                             instance_id, node["node_id"])
    result = {"scanned": len(nodes), "committed": committed, "failed": failed,
              "more_possible": len(nodes) == limit}
    logger.info("Cognition recovery %s instance=%s", result, instance_id)
    return result



async def finish_deferred_cognition(db, *, instance_id: UUID,
                                    enrichment_limit: int = 4,
                                    goal_batch_limit: int = 64) -> dict:
    """Complete historical inference before making recovered goals authoritative.

    The API returns explicit pending/blocked states; unavailable local inference
    must never silently turn an unreviewed source into authoritative memory.
    """
    if await missing_cognition_nodes(db, instance_id=instance_id, limit=1):
        return {"status": "source_cognition_pending", "enrichment_completed": 0,
                "goal_reconciliation": None}
    awaiting = await deferred_enrichment_nodes(
        db, instance_id=instance_id, limit=enrichment_limit,
    )
    if awaiting:
        from aios_app.epistemic.message_cognition_enrichment import MessageCognitionEnricher
        enricher = MessageCognitionEnricher(db)
        for node in awaiting:
            await enricher.run(instance_id=instance_id, node_id=node["node_id"])
    remaining = await deferred_enrichment_nodes(
        db, instance_id=instance_id, limit=enrichment_limit,
    )
    before_ids = {str(node["node_id"]) for node in awaiting}
    after_ids = {str(node["node_id"]) for node in remaining}
    completed = len(before_ids - after_ids)
    if remaining:
        # Do not conflate a missing provider with a syntactically invalid
        # response. Report the latest persisted attempt without disclosing
        # prompts, provider secrets or model-generated narrative.
        latest = await db.fetchrow(
            """SELECT request_id,status,validation_error,error,created_at
               FROM aios.inference_request
               WHERE instance_id=$1 AND worker_class='message_cognition'
                 AND created_at >= now()-interval '10 minutes'
               ORDER BY created_at DESC LIMIT 1""", instance_id,
        )
        outcome = "source_inference_pending" if completed else (
            "source_inference_invalid_response" if latest and latest["status"] == "invalid"
            else "source_inference_failed" if latest and latest["status"] == "failed"
            else "source_inference_unavailable"
        )
        return {
            "status": outcome, "enrichment_completed": completed,
            "remaining_enrichment_sample": len(remaining),
            "latest_inference": ({
                "request_id": str(latest["request_id"]),
                "status": latest["status"],
                "validation_error": latest["validation_error"],
                "error": latest["error"],
                "created_at": str(latest["created_at"]),
            } if latest else None),
            "goal_reconciliation": None,
        }
    from aios_app.epistemic.deferred_goal_reconciliation import reconcile_deferred_goals
    outcome = await reconcile_deferred_goals(
        db, instance_id=instance_id, limit=goal_batch_limit,
    )
    return {"status": "goal_reconciliation_pending" if outcome["remaining"] else "ready",
            "enrichment_completed": completed, "goal_reconciliation": outcome}


async def _cli() -> None:
    """Operator recovery of old gaps without a new roleplay turn."""
    import argparse
    import json
    from aios_app.config import settings
    from aios_app.db import Database

    parser = argparse.ArgumentParser(description="Recover missing DAG message cognition")
    parser.add_argument("--instance-id", required=True, type=UUID)
    parser.add_argument("--max-batches", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.max_batches <= 64:
        parser.error("--max-batches must be 1..64")
    db = Database(settings.db_dsn, min_size=1, max_size=2)
    await db.connect()
    try:
        for _ in range(args.max_batches):
            report = await recover_missing_cognition(db, instance_id=args.instance_id)
            print(json.dumps(report, sort_keys=True), flush=True)
            if not report["more_possible"] or report["committed"] == 0:
                break
        for _ in range(args.max_batches):
            finished = await finish_deferred_cognition(
                db, instance_id=args.instance_id,
            )
            print(json.dumps(finished, default=str, sort_keys=True), flush=True)
            if finished["status"] not in {
                "source_inference_pending", "goal_reconciliation_pending",
            }:
                break
            if finished["status"] == "source_inference_pending" and not finished["enrichment_completed"]:
                break
            reviewed = finished.get("goal_reconciliation") or {}
            if finished["status"] == "goal_reconciliation_pending" and not reviewed.get("considered"):
                break
    finally:
        await db.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_cli())
