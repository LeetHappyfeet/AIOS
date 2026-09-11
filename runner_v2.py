from __future__ import annotations

import asyncio
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
    try:
        await base.run_runner(poll_interval=poll_interval)
    finally:
        projector.cancel()
        await asyncio.gather(projector, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_runner())
