"""Physical resource governor for AIOS pipeline workers.

This layer sits above logical pipeline routing and prevents background work
from exhausting the host while foreground cognition remains responsive.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


@dataclass(frozen=True)
class ResourceBudget:
    cpu_tokens: int = 16


@dataclass(frozen=True)
class AdmissionCost:
    tokens: int


COSTS = {
    "resolve_claim_context": AdmissionCost(2),
    "normalize_proposition": AdmissionCost(1),
    "decompose_claim_frames": AdmissionCost(1),
    "derive_claim_topology": AdmissionCost(3),
    "derive_character_acquisition_topology": AdmissionCost(3),
    "rdf_epistemic_project": AdmissionCost(4),
    "project_semantic_scope": AdmissionCost(4),
    "compact_character_world_epistemic": AdmissionCost(5),
    "semantic_index": AdmissionCost(6),
}


class ResourceGovernor:
    """Admission controller for physical host resources.

    It intentionally does not alter correctness semantics. A rejected job is
    only delayed; ownership, leases, and partitioning remain handled by the
    pipeline scheduler.
    """

    def __init__(self, budget: ResourceBudget | None = None):
        self.budget = budget or ResourceBudget()
        self._held = 0
        self._leases: dict[str, int] = {}
        self._last_sample = 0.0
        self._cpu = 0.0

    def _sample_cpu(self) -> float:
        now = monotonic()
        if now - self._last_sample > 2:
            self._last_sample = now
            if psutil:
                self._cpu = float(psutil.cpu_percent(interval=None))
        return self._cpu

    def admit(self, job: dict) -> bool:
        job_type = str(job.get("job_type") or "")
        lane = str(job.get("scheduling_lane") or "DEFAULT")
        cost = COSTS.get(job_type, AdmissionCost(1)).tokens

        cpu = self._sample_cpu()

        # Protect LIVE cognition. Background work yields first.
        if cpu >= 90 and lane == "BACKGROUND":
            return False

        # Structural work yields when LIVE semantic work is under pressure.
        if lane == "STRUCTURAL" and self._held + cost > self.budget.cpu_tokens - 4:
            return False

        if self._held + cost > self.budget.cpu_tokens:
            return False

        job_id = str(job.get("job_id") or "")
        self._held += cost
        if job_id:
            self._leases[job_id] = cost
        return True

    def release(self, job_id: str) -> None:
        cost = self._leases.pop(str(job_id), 0)
        self._held = max(0, self._held - cost)

    @property
    def utilization(self) -> float:
        return self._held / max(1, self.budget.cpu_tokens)
