from __future__ import annotations

from typing import Protocol, Sequence

from .types import CausalCandidate, CausalEvaluation, CausalState


class CausalDomain(Protocol):
    """Pure policy boundary for one deterministic state domain."""

    domain_id: str

    def evaluate(
        self,
        state: CausalState | None,
        candidate: CausalCandidate,
        *,
        rules: Sequence[dict],
    ) -> CausalEvaluation:
        ...
