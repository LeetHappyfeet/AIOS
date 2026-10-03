"""Independent, bounded restart recovery for deferred historical cognition.

Runs on the existing pipeline runner cadence. Candidate discovery is scoped to
actual DAG ancestors of each instance's current source head, not all historical
commits. It never changes cognition receipts or directly calls inference.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from aios_app.pipeline.jobs import enqueue_job

logger = logging.getLogger("aios.epistemic.cognition_recovery_sweep")

# A failed endpoint must not be hammered by periodic rediscovery. Successful
# completions are handled by the normal cognition receipt flags.
SWEEP_INTERVAL_SECONDS = 60
SWEEP_LIMIT = 16


async def discover_abandoned_enrichment(db, *, limit: int = SWEEP_LIMIT):
    if not 1 <= limit <= SWEEP_LIMIT:
        raise ValueError("Cognition recovery sweep must remain bounded (1..16)")
    return await db.fetch(
        """
        WITH RECURSIVE pending_instances AS (
            SELECT DISTINCT c.instance_id
            FROM aios.message_cognitive_commit c
            JOIN aios.character_runtime_state rs ON rs.instance_id=c.instance_id
            WHERE c.summary->>'historical_catchup'='true'
              AND c.summary->>'enrichment_deferred'='true'
              AND c.summary->>'enrichment_pending'='true'
              AND rs.source_head_node_id IS NOT NULL
              AND rs.source_timeline_id IS NOT NULL
        ), source_chain AS (
            SELECT pi.instance_id, head.timeline_id, head.node_id,
                   ARRAY[head.node_id]::uuid[] AS visited, 0 AS depth
            FROM pending_instances pi
            JOIN aios.character_runtime_state rs ON rs.instance_id=pi.instance_id
            JOIN aios.dag_node head ON head.node_id=rs.source_head_node_id
                                   AND head.timeline_id=rs.source_timeline_id
            UNION ALL
            SELECT chain.instance_id, parent.timeline_id, parent.node_id,
                   chain.visited || parent.node_id, chain.depth+1
            FROM source_chain chain
            JOIN aios.dag_edge de ON de.timeline_id=chain.timeline_id
                                  AND de.child_node_id=chain.node_id
            JOIN aios.dag_node parent ON parent.timeline_id=de.timeline_id
                                      AND parent.node_id=de.parent_node_id
            WHERE chain.depth<4096 AND NOT parent.node_id=ANY(chain.visited)
        ), candidates AS (
            SELECT c.instance_id, MIN(c.event_id) AS oldest_pending_event
            FROM aios.message_cognitive_commit c
            JOIN source_chain chain ON chain.instance_id=c.instance_id
                                   AND chain.node_id=c.node_id
            WHERE c.summary->>'historical_catchup'='true'
              AND c.summary->>'enrichment_deferred'='true'
              AND c.summary->>'enrichment_pending'='true'
            GROUP BY c.instance_id
        )
        SELECT candidates.instance_id,candidates.oldest_pending_event
        FROM candidates
        WHERE NOT EXISTS (
            SELECT 1 FROM aios.inference_request ir
            WHERE ir.instance_id=candidates.instance_id
              AND ir.worker_class='message_cognition' AND ir.status='running'
              AND (ir.lease_expires_at > now()
                   OR (ir.lease_expires_at IS NULL
                       AND ir.created_at > now()-interval '2 minutes'))
        )
          AND NOT EXISTS (
            SELECT 1 FROM aios.pipeline_job pj
            WHERE pj.job_type IN ('message_cognition_catchup',
                                  'message_cognition_enrichment')
              AND pj.payload->>'instance_id'=candidates.instance_id::text
              AND pj.status IN ('queued','running')
        )
          AND NOT EXISTS (
            SELECT 1 FROM (
                SELECT COALESCE(ir.completed_at,ir.created_at) AS attempt_finished_at,
                       ir.status,
                       (SELECT count(*) FROM aios.inference_request previous
                        WHERE previous.instance_id=ir.instance_id
                          AND previous.worker_class='message_cognition'
                          AND previous.status IN ('failed','invalid')
                          AND previous.created_at > now()-interval '1 hour'
                       ) AS recent_failures
                FROM aios.inference_request ir
                WHERE ir.instance_id=candidates.instance_id
                  AND ir.worker_class='message_cognition'
                ORDER BY ir.created_at DESC LIMIT 1
            ) last
            WHERE last.status IN ('failed','invalid')
              AND last.attempt_finished_at + (
                LEAST(900, 60 * power(2, LEAST(last.recent_failures,4))) *
                interval '1 second'
              ) > now()
        )
        ORDER BY candidates.oldest_pending_event ASC, candidates.instance_id
        LIMIT $1
        """, limit,
    )


async def enqueue_abandoned_enrichment(db, *, limit: int = SWEEP_LIMIT) -> int:
    """Discover and schedule; the ordinary catch-up handler owns reconciliation."""
    candidates = await discover_abandoned_enrichment(db, limit=limit)
    queued = 0
    for row in candidates:
        instance_id = str(row["instance_id"])
        # The enqueue layer performs a final atomic dedupe to resolve the race
        # between a regular catch-up continuation and this independent sweep.
        job_id = await enqueue_job(
            db, job_type="message_cognition_catchup",
            payload={"instance_id":instance_id, "retry_count":0,
                     "recovery_source":"deferred_enrichment_sweep"},
            priority=65,
        )
        if job_id:
            queued += 1
            logger.info("Recovered abandoned historical cognition instance=%s "
                        "oldest_event=%s job=%s",
                        instance_id, row["oldest_pending_event"], job_id)
    return queued
