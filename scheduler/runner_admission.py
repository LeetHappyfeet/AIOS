"""Runner-facing adapter for the process-local resource governor."""
from __future__ import annotations

import logging
from time import monotonic
from typing import Any

from aios_app.scheduler.resource_governor import ResourceGovernor

logger = logging.getLogger("aios.scheduler.kernel")
_last_report: dict[str, float] = {}
_suppressed: dict[str, int] = {}


async def admit_job(
    governor: ResourceGovernor,
    job: dict[str, Any],
) -> bool:
    decision = await governor.admit(job)
    if not decision.allowed:
        reason = decision.reason
        now = monotonic()
        _suppressed[reason] = _suppressed.get(reason, 0) + 1
        if now - _last_report.get(reason, float("-inf")) >= 60.0:
            logger.info(
                "Resource governor deferred jobs reason=%s count=%d cpu=%.1f%% latest_type=%s",
                reason,
                _suppressed.pop(reason),
                decision.cpu_percent,
                job.get("job_type"),
            )
            _last_report[reason] = now
    return decision.allowed


async def release_job(
    governor: ResourceGovernor,
    job: dict[str, Any],
) -> None:
    await governor.release(job.get("job_id"))
