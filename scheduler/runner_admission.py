"""Runner-facing adapter for the process-local resource governor."""
from __future__ import annotations

import logging
from typing import Any

from aios_app.scheduler.resource_governor import ResourceGovernor

logger = logging.getLogger("aios.scheduler.kernel")


async def admit_job(
    governor: ResourceGovernor,
    job: dict[str, Any],
) -> bool:
    decision = await governor.admit(job)
    if not decision.allowed:
        logger.info(
            "Resource governor deferred job=%s type=%s reason=%s cpu=%.1f%%",
            job.get("job_id"),
            job.get("job_type"),
            decision.reason,
            decision.cpu_percent,
        )
    return decision.allowed


async def release_job(
    governor: ResourceGovernor,
    job: dict[str, Any],
) -> None:
    await governor.release(job.get("job_id"))
