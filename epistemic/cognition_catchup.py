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
        """SELECT dn.node_id, dn.event_id
           FROM aios.character_runtime_state rs
           JOIN aios.dag_node head
             ON head.node_id=rs.source_head_node_id
            AND head.timeline_id=rs.source_timeline_id
           JOIN aios.dag_node dn
             ON dn.timeline_id=rs.source_timeline_id
            AND dn.event_id<=head.event_id
           WHERE rs.instance_id=$1
             AND dn.message_text IS NOT NULL
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
