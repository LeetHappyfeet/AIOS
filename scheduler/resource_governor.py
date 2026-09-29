"""Process-local physical resource admission for pipeline workers.

Logical queue/lane policy remains in the runner.  This governor only bounds
concurrent work admitted by this runner and makes background work yield when
host CPU is saturated.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from time import monotonic
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


@dataclass(frozen=True)
class ResourceBudget:
    cpu_tokens: int = max(4, int(os.getenv("AIOS_RUNNER_CPU_TOKENS", "16")))


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str
    cost: int = 0
    cpu_percent: float = 0.0


COSTS = {
    "resolve_claim_context": 2,
    "normalize_proposition": 1,
    "materialize_event_occurrences": 2,
    "project_character_knowledge": 2,
    "decompose_claim_frames": 1,
    "derive_claim_topology": 3,
    "derive_character_acquisition_topology": 3,
    "rdf_epistemic_project": 4,
    "project_semantic_scope": 4,
    "compact_character_world_epistemic": 5,
}


class ResourceGovernor:
    """Long-lived, concurrency-safe admission controller for one runner."""

    def __init__(self, budget: ResourceBudget | None = None):
        self.budget = budget or ResourceBudget()
        self._held = 0
        self._leases: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._last_sample = 0.0
        self._cpu = 0.0
        self._cpu_initialized = False

    def _sample_cpu(self) -> float:
        now = monotonic()
        if now - self._last_sample < 2.0:
            return self._cpu
        self._last_sample = now
        if psutil is not None:
            # The first non-blocking psutil sample is baseline-only. Do not
            # throttle from it; subsequent samples represent the elapsed window.
            value = float(psutil.cpu_percent(interval=None))
            if self._cpu_initialized:
                self._cpu = value
            else:
                self._cpu_initialized = True
        else:
            # Portable fallback: normalized one-minute load is coarse but lets a
            # separate semantic-index/Postgres CPU surge make background yield.
            try:
                cpus = max(1, os.cpu_count() or 1)
                self._cpu = min(100.0, (os.getloadavg()[0] / cpus) * 100.0)
            except (AttributeError, OSError):
                self._cpu = 0.0
        return self._cpu

    async def admit(self, job: dict[str, Any]) -> AdmissionDecision:
        job_id = str(job.get("job_id") or "")
        lane = str(job.get("scheduling_lane") or "DEFAULT")
        job_type = str(job.get("job_type") or "")
        cost = int(COSTS.get(job_type, 1))
        cpu = self._sample_cpu()

        async with self._lock:
            if job_id and job_id in self._leases:
                return AdmissionDecision(True, "already-admitted", self._leases[job_id], cpu)

            if cpu >= 90.0 and lane in {"BACKGROUND", "DEFAULT"}:
                return AdmissionDecision(False, "host-cpu-critical", cost, cpu)

            # Preserve the dedicated structural writer, but do not let heavy
            # structural work consume the reserve kept for LIVE progression.
            if lane == "STRUCTURAL" and self._held + cost > self.budget.cpu_tokens - 4:
                return AdmissionDecision(False, "live-token-reserve", cost, cpu)

            if self._held + cost > self.budget.cpu_tokens:
                return AdmissionDecision(False, "runner-token-budget", cost, cpu)

            self._held += cost
            if job_id:
                self._leases[job_id] = cost
            return AdmissionDecision(True, "admitted", cost, cpu)

    async def release(self, job_id: Any) -> None:
        async with self._lock:
            cost = self._leases.pop(str(job_id), 0)
            self._held = max(0, self._held - cost)

    @property
    def utilization(self) -> float:
        return self._held / max(1, self.budget.cpu_tokens)
