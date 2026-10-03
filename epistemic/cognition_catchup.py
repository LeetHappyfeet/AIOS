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
    finally:
        await db.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_cli())
