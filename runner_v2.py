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
        return str(payload["scope_key"])
    if job_type == "project_character_knowledge" and payload.get("live_instance_id"):
        return f"instance:{payload['live_instance_id']}"
    return await _original_resolve_partition_key(db, job)


base._resolve_partition_key = _resolve_partition_key


def _emit_telemetry(payload: dict[str, Any]) -> None:
    print(
        "AIOS_TELEMETRY " + json.dumps(payload, separators=(",", ":"), default=str),
        flush=True,
    )


def _short(value: Any, width: int = 8) -> str:
    text = str(value or "")
    return text[:width] if text else ""


async def _work_subject(db: Database, row: Any) -> dict[str, Any]:
    payload = dict(row["payload"] or {})
    partition_key = str(row["partition_key"] or "")

    scope_key = str(payload.get("scope_key") or "")
    for value in (scope_key, partition_key):
        if value.startswith("char:"):
            return {"subject_type": "character", "subject_id": value.split(":", 1)[1]}
        if value.startswith("world:"):
            return {"subject_type": "world", "subject_id": value}
        if value.startswith("source:"):
            return {"subject_type": "source", "subject_id": value.split(":", 1)[1]}

    if payload.get("character_id"):
        return {"subject_type": "character", "subject_id": str(payload["character_id"])}

    live_instance_id = payload.get("live_instance_id") or payload.get("instance_id")
    if live_instance_id:
        resolved = await db.fetchrow(
            """
            SELECT character_id
            FROM aios.character_instance
            WHERE instance_id=$1::uuid
            """,
            live_instance_id,
        )
        if resolved and resolved["character_id"]:
            return {
                "subject_type": "character",
                "subject_id": str(resolved["character_id"]),
                "instance_id": str(live_instance_id),
            }

    acquisition_id = payload.get("acquisition_id")
    if acquisition_id:
        resolved = await db.fetchrow(
            """
            SELECT ci.character_id
            FROM aios.knowledge_acquisition_event kae
            JOIN aios.character_instance ci ON ci.instance_id=kae.instance_id
            WHERE kae.acquisition_id=$1::uuid
            """,
            acquisition_id,
        )
        if resolved and resolved["character_id"]:
            return {"subject_type": "character", "subject_id": str(resolved["character_id"])}

    claim_id = payload.get("claim_id")
    if claim_id:
        resolved = await db.fetchrow(
            """
            SELECT origin_character_id, world_id, source_id
            FROM aios.claim_context_resolution
            WHERE claim_id=$1::uuid
            """,
            claim_id,
        )
        if resolved:
            if resolved["origin_character_id"]:
                return {
                    "subject_type": "character",
                    "subject_id": str(resolved["origin_character_id"]),
                }
            if resolved["world_id"]:
                return {
                    "subject_type": "world",
                    "subject_id": f"world:{resolved['world_id']}",
                }
            if resolved["source_id"]:
                return {"subject_type": "source", "subject_id": str(resolved["source_id"])}

    for key, subject_type in (
        ("world_id", "world"),
        ("node_id", "node"),
        ("section_id", "section"),
        ("claim_id", "claim"),
        ("assertion_id", "assertion"),
        ("acquisition_id", "acquisition"),
    ):
        if payload.get(key):
            value = str(payload[key])
            return {"subject_type": subject_type, "subject_id": value, "short_id": _short(value)}

    if partition_key:
        return {"subject_type": "partition", "subject_id": partition_key}
    return {"subject_type": "global", "subject_id": "global"}


async def _describe_work_rows(db: Database, rows: list[Any]) -> list[dict[str, Any]]:
    described: list[dict[str, Any]] = []
    for row in rows:
        subject = await _work_subject(db, row)
        described.append(
            {
                "job_id": str(row["job_id"]),
                "job_type": str(row["job_type"]),
                "resource": str(row["resource_class"]),
                "lane": str(row["scheduling_lane"]),
                "running_s": round(float(row.get("running_seconds", 0.0) or 0.0), 2)
                if hasattr(row, "get")
                else round(float(row["running_seconds"] or 0.0), 2),
                **subject,
            }
        )
    return described


def _candidate_state(
    *,
    queued: int,
    oldest_s: float,
    arrivals_per_s: float,
    done_per_s: float,
    queue_velocity: float,
    failures: int = 0,
    live: bool = False,
) -> str:
    if failures:
        return "DEGRADED"
    if queued == 0 or (live and oldest_s <= 2.0):
        return "READY" if live else "CAUGHT_UP"

    scale = max(arrivals_per_s, done_per_s, 0.25)
    net_ratio = (done_per_s - arrivals_per_s) / scale
    if net_ratio >= 0.10 and queue_velocity <= 0.10:
        return "DRAINING"
    if net_ratio <= -0.10 and queue_velocity >= -0.10:
        return "LAGGING" if live else "FALLING_BEHIND"
    return "BUSY"


def _stabilize_state(
    key: str,
    candidate: str,
    stable: dict[str, str],
    pending: dict[str, tuple[str, int]],
) -> str:
    current = stable.get(key)
    if current is None:
        stable[key] = candidate
        return candidate
    if candidate == current:
        pending.pop(key, None)
        return current
    if candidate in {"DEGRADED", "CAUGHT_UP", "READY"}:
        stable[key] = candidate
        pending.pop(key, None)
        return candidate

    previous_candidate, count = pending.get(key, ("", 0))
    count = count + 1 if previous_candidate == candidate else 1
    pending[key] = (candidate, count)
    if count >= 3:
        stable[key] = candidate
        pending.pop(key, None)
    return stable[key]


async def _pipeline_telemetry_loop(interval: float = 5.0) -> None:
    """Publish compact queue, stage, and active-subject telemetry."""
    db = Database(
        settings.db_dsn,
        min_size=1,
        max_size=max(2, settings.db_pool_min_size),
    )
    await db.connect()
    previous_queued: int | None = None
    previous_live_queued: int | None = None
    previous_at: float | None = None
    stable_states: dict[str, str] = {}
    pending_states: dict[str, tuple[str, int]] = {}
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
                stage_rows = await db.fetch(
                    """
                    SELECT
                        job_type,
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
                    GROUP BY job_type, resource_class, scheduling_lane
                    HAVING COUNT(*) FILTER (WHERE status='queued' AND run_after <= now()) > 0
                        OR COUNT(*) FILTER (WHERE status='running') > 0
                    ORDER BY oldest_seconds DESC, queued DESC
                    """
                )
                rates = await db.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE created_at >= now() - interval '30 seconds')::integer AS arrivals_30s,
                        COUNT(*) FILTER (
                            WHERE status='done' AND updated_at >= now() - interval '30 seconds'
                        )::integer AS done_30s,
                        COUNT(*) FILTER (
                            WHERE scheduling_lane='LIVE'
                              AND created_at >= now() - interval '30 seconds'
                        )::integer AS live_arrivals_30s,
                        COUNT(*) FILTER (
                            WHERE scheduling_lane='LIVE'
                              AND status='done'
                              AND updated_at >= now() - interval '30 seconds'
                        )::integer AS live_done_30s,
                        COUNT(*) FILTER (
                            WHERE status='failed' AND updated_at >= now() - interval '60 seconds'
                        )::integer AS failed_60s
                    FROM aios.pipeline_job
                    WHERE created_at >= now() - interval '60 seconds'
                       OR updated_at >= now() - interval '60 seconds'
                    """
                )
                running_rows = await db.fetch(
                    """
                    SELECT
                        job_id, job_type, resource_class, scheduling_lane,
                        partition_key, payload,
                        COALESCE(EXTRACT(EPOCH FROM (now() - claimed_at)), 0)::double precision AS running_seconds
                    FROM aios.pipeline_job
                    WHERE status='running'
                    ORDER BY claimed_at ASC NULLS LAST
                    LIMIT 12
                    """
                )
                recent_rows = await db.fetch(
                    """
                    SELECT
                        job_id, job_type, resource_class, scheduling_lane,
                        partition_key, payload,
                        COALESCE(EXTRACT(EPOCH FROM (now() - updated_at)), 0)::double precision AS running_seconds
                    FROM aios.pipeline_job
                    WHERE status='done'
                      AND updated_at >= now() - interval '20 seconds'
                    ORDER BY updated_at DESC
                    LIMIT 8
                    """
                )

                lanes: list[dict[str, Any]] = []
                total_queued = total_running = live_queued = live_running = 0
                oldest = live_oldest = 0.0
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
                queue_velocity = live_queue_velocity = 0.0
                if previous_at is not None:
                    elapsed = max(0.001, now - previous_at)
                    if previous_queued is not None:
                        queue_velocity = (total_queued - previous_queued) / elapsed
                    if previous_live_queued is not None:
                        live_queue_velocity = (live_queued - previous_live_queued) / elapsed
                previous_queued = total_queued
                previous_live_queued = live_queued
                previous_at = now

                arrivals_per_s = float((rates["arrivals_30s"] if rates else 0) or 0) / 30.0
                done_per_s = float((rates["done_30s"] if rates else 0) or 0) / 30.0
                live_arrivals_per_s = float((rates["live_arrivals_30s"] if rates else 0) or 0) / 30.0
                live_done_per_s = float((rates["live_done_30s"] if rates else 0) or 0) / 30.0
                failed_60s = int((rates["failed_60s"] if rates else 0) or 0)

                state = _stabilize_state(
                    "global",
                    _candidate_state(
                        queued=total_queued,
                        oldest_s=oldest,
                        arrivals_per_s=arrivals_per_s,
                        done_per_s=done_per_s,
                        queue_velocity=queue_velocity,
                        failures=failed_60s,
                    ),
                    stable_states,
                    pending_states,
                )
                live_state = _stabilize_state(
                    "live",
                    _candidate_state(
                        queued=live_queued,
                        oldest_s=live_oldest,
                        arrivals_per_s=live_arrivals_per_s,
                        done_per_s=live_done_per_s,
                        queue_velocity=live_queue_velocity,
                        live=True,
                    ),
                    stable_states,
                    pending_states,
                )

                stage_backlog = [
                    {
                        "job_type": str(row["job_type"]),
                        "resource": str(row["resource_class"]),
                        "lane": str(row["scheduling_lane"]),
                        "queued": int(row["queued"] or 0),
                        "running": int(row["running"] or 0),
                        "oldest_s": round(float(row["oldest_seconds"] or 0.0), 2),
                    }
                    for row in stage_rows
                ]
                active_work = await _describe_work_rows(db, list(running_rows))
                recent_work = await _describe_work_rows(db, list(recent_rows))

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
                        "live_arrivals_per_s": round(live_arrivals_per_s, 2),
                        "live_done_per_s": round(live_done_per_s, 2),
                        "queue_velocity_per_s": round(queue_velocity, 2),
                        "live_queue_velocity_per_s": round(live_queue_velocity, 2),
                        "failed_60s": failed_60s,
                        "lanes": lanes,
                        "stage_backlog": stage_backlog,
                        "active_work": active_work,
                        "recent_work": recent_work,
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
