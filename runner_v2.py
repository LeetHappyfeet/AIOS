from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from aios_app import runner as base
from aios_app.config import settings
from aios_app.db import Database
from aios_app.epistemic.topology_projection import (
    enqueue_dirty_scope_jobs,
    project_semantic_scope,
)
from aios_app.pipeline.jobs import enqueue_job

logger = logging.getLogger("aios.pipeline.runner")


async def handle_project_semantic_scope(db: Database, job: Dict[str, Any]) -> None:
    scope_key = str((job.get("payload") or {}).get("scope_key") or "").strip()
    if not scope_key:
        raise ValueError("project_semantic_scope requires scope_key")
    fuseki = base.FusekiClient(settings.fuseki_base_url)
    await project_semantic_scope(db, fuseki, scope_key=scope_key)


base.JOB_HANDLERS["project_semantic_scope"] = handle_project_semantic_scope

_original_resolve_partition_key = base._resolve_partition_key


async def _resolve_partition_key(db: Database, job: Dict[str, Any]) -> str:
    payload = job.get("payload") or {}
    job_type = str(job.get("job_type") or "")
    if job_type == "project_semantic_scope" and payload.get("scope_key"):
        # Reuse the exact semantic-scope advisory lock used by topology mutation.
        return str(payload["scope_key"])
    if job_type == "project_character_knowledge" and payload.get("live_instance_id"):
        # A live character projection should not convoy behind unrelated global
        # knowledge projection work.
        return f"instance:{payload['live_instance_id']}"
    return await _original_resolve_partition_key(db, job)


base._resolve_partition_key = _resolve_partition_key


def _emit_telemetry(payload: dict[str, Any]) -> None:
    print(
        "AIOS_TELEMETRY " + json.dumps(payload, separators=(",", ":"), default=str),
        flush=True,
    )


async def _pipeline_telemetry_loop(interval: float = 5.0) -> None:
    """Publish compact queue/freshness telemetry for the launcher.

    This is deliberately read-only. Scheduling remains entirely owned by the
    pipeline runner; launch.py only consumes these snapshots for display.
    """
    db = Database(
        settings.db_dsn,
        min_size=1,
        max_size=max(2, settings.db_pool_min_size),
    )
    await db.connect()
    previous_queued: int | None = None
    previous_at: float | None = None
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                rows = await db.fetch(
                    """
                    SELECT
                        resource_class,
                        scheduling_lane,
                        COUNT(*) FILTER (WHERE status='queued' AND run_after <= now())::integer AS queued,
                        COUNT(*) FILTER (WHERE status='running')::integer AS running,
                        COALESCE(
                            EXTRACT(EPOCH FROM (
                                now() - MIN(created_at) FILTER (
                                    WHERE status='queued' AND run_after <= now()
                                )
                            )),
                            0
                        )::double precision AS oldest_seconds
                    FROM aios.pipeline_job
                    WHERE status IN ('queued','running')
                    GROUP BY resource_class, scheduling_lane
                    ORDER BY resource_class, scheduling_lane
                    """
                )
                rates = await db.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE created_at >= now() - interval '30 seconds'
                        )::integer AS arrivals_30s,
                        COUNT(*) FILTER (
                            WHERE status='done'
                              AND updated_at >= now() - interval '30 seconds'
                        )::integer AS done_30s,
                        COUNT(*) FILTER (
                            WHERE status='failed'
                              AND updated_at >= now() - interval '60 seconds'
                        )::integer AS failed_60s
                    FROM aios.pipeline_job
                    WHERE created_at >= now() - interval '60 seconds'
                       OR updated_at >= now() - interval '60 seconds'
                    """
                )

                lanes: list[dict[str, Any]] = []
                total_queued = 0
                total_running = 0
                oldest = 0.0
                live_queued = 0
                live_running = 0
                live_oldest = 0.0
                for row in rows:
                    queued = int(row["queued"] or 0)
                    running = int(row["running"] or 0)
                    age = float(row["oldest_seconds"] or 0.0)
                    lane = str(row["scheduling_lane"])
                    total_queued += queued
                    total_running += running
                    oldest = max(oldest, age)
                    if lane == "LIVE":
                        live_queued += queued
                        live_running += running
                        live_oldest = max(live_oldest, age)
                    lanes.append(
                        {
                            "resource": str(row["resource_class"]),
                            "lane": lane,
                            "queued": queued,
                            "running": running,
                            "oldest_s": round(age, 2),
                        }
                    )

                now = loop.time()
                queue_velocity = 0.0
                if previous_queued is not None and previous_at is not None:
                    elapsed = max(0.001, now - previous_at)
                    queue_velocity = (total_queued - previous_queued) / elapsed
                previous_queued = total_queued
                previous_at = now

                arrivals_per_s = float((rates["arrivals_30s"] if rates else 0) or 0) / 30.0
                done_per_s = float((rates["done_30s"] if rates else 0) or 0) / 30.0
                failed_60s = int((rates["failed_60s"] if rates else 0) or 0)

                if failed_60s:
                    state = "DEGRADED"
                elif total_queued == 0:
                    state = "CAUGHT_UP"
                elif done_per_s > arrivals_per_s + 0.05 or queue_velocity < -0.05:
                    state = "DRAINING"
                elif arrivals_per_s > done_per_s + 0.05 and queue_velocity > 0.05:
                    state = "FALLING_BEHIND"
                else:
                    state = "BUSY"

                live_state = (
                    "READY"
                    if live_queued == 0 or live_oldest <= 2.0
                    else ("DRAINING" if done_per_s >= arrivals_per_s else "LAGGING")
                )
                drain_seconds = None
                net_drain = done_per_s - arrivals_per_s
                if total_queued and net_drain > 0.05:
                    drain_seconds = round(total_queued / net_drain, 1)

                _emit_telemetry(
                    {
                        "service": "pipeline",
                        "state": state,
                        "live_state": live_state,
                        "queued": total_queued,
                        "running": total_running,
                        "oldest_s": round(oldest, 2),
                        "live_queued": live_queued,
                        "live_running": live_running,
                        "live_oldest_s": round(live_oldest, 2),
                        "arrivals_per_s": round(arrivals_per_s, 2),
                        "done_per_s": round(done_per_s, 2),
                        "queue_velocity_per_s": round(queue_velocity, 2),
                        "failed_60s": failed_60s,
                        "drain_seconds": drain_seconds,
                        "lanes": lanes,
                    }
                )
            except Exception:
                logger.exception("Failed to collect pipeline telemetry")
            await asyncio.sleep(interval)
    finally:
        await db.close()


async def _projection_scheduler_loop() -> None:
    db = Database(
        settings.db_dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    await db.connect()
    try:
        while True:
            try:
                created = await enqueue_dirty_scope_jobs(db, limit=64)
                if created:
                    logger.debug("Queued %d coalesced semantic scope projections", created)
            except Exception:
                logger.exception("Failed to schedule dirty semantic topology scopes")
            await asyncio.sleep(1.0)
    finally:
        await db.close()


async def run_runner(poll_interval: float = 1.0) -> None:
    projector = asyncio.create_task(
        _projection_scheduler_loop(),
        name="semantic-scope-projection-scheduler",
    )
    telemetry = asyncio.create_task(
        _pipeline_telemetry_loop(),
        name="pipeline-telemetry",
    )
    try:
        await base.run_runner(poll_interval=poll_interval)
    finally:
        projector.cancel()
        telemetry.cancel()
        await asyncio.gather(projector, telemetry, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_runner())
