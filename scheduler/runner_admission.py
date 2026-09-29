"""Runner admission helpers for the AIOS resource kernel.

This module keeps scheduling policy outside pipeline handlers. The runner calls
admit before invoking a claimed job and release in finally blocks.
"""

from __future__ import annotations

import logging
from typing import Any

from aios_app.scheduler.resource_governor import ResourceGovernor

logger = logging.getLogger("aios.scheduler.kernel")


async def admit_job(job: dict[str, Any]) -> tuple[bool, Any]:
    governor = ResourceGovernor()
    decision = await governor.admit(
        resource_class=str(job.get("resource_class") or "UNKNOWN"),
        scheduling_lane=str(job.get("scheduling_lane") or "DEFAULT"),
        job_type=str(job.get("job_type") or "unknown"),
    )
    if not decision.allowed:
        logger.info(
            "Resource governor deferred job=%s type=%s reason=%s",
            job.get("job_id"),
            job.get("job_type"),
            decision.reason,
        )
    return decision.allowed, governor


async def release_job(governor: Any, job: dict[str, Any]) -> None:
    if governor is not None:
        await governor.release(
            resource_class=str(job.get("resource_class") or "UNKNOWN"),
            scheduling_lane=str(job.get("scheduling_lane") or "DEFAULT"),
        )
