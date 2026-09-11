from __future__ import annotations

from typing import Sequence

from ..types import (
    AdmissionDecision,
    CausalCandidate,
    CausalEvaluation,
    CausalState,
)


class ScalarStateDomain:
    """Generic typed deterministic variable domain for sensors/simulators.

    The schema/units live in candidate parameters or plugin registration.  This
    domain only protects branch ordering and prevents incompatible concurrent
    writes from silently overwriting one another.
    """

    domain_id = "world.scalar"

    def evaluate(
        self,
        state: CausalState | None,
        candidate: CausalCandidate,
        *,
        rules: Sequence[dict],
    ) -> CausalEvaluation:
        before = state.value if state else None
        after = candidate.value

        if state is None:
            return CausalEvaluation(
                AdmissionDecision.ADMITTED,
                "scalar.initial",
                "Initial deterministic value is admissible.",
                before=None,
                after=after,
                delta={candidate.state_key: [None, after]},
            )

        if before == after:
            return CausalEvaluation(
                AdmissionDecision.ADMITTED,
                "scalar.idempotent",
                "Value agrees with canonical state.",
                before=before,
                after=after,
                delta={},
            )

        same_node = bool(
            candidate.dag_node_id
            and state.last_node_id
            and candidate.dag_node_id == state.last_node_id
        )
        same_time = bool(
            candidate.occurred_at
            and state.occurred_at
            and candidate.occurred_at == state.occurred_at
        )
        if same_node or same_time:
            return CausalEvaluation(
                AdmissionDecision.CONFLICT,
                "scalar.concurrent_conflict",
                "Two deterministic values disagree at the same causal coordinate.",
                before=before,
                after=after,
            )

        if (
            candidate.occurred_at
            and state.occurred_at
            and candidate.occurred_at < state.occurred_at
        ):
            return CausalEvaluation(
                AdmissionDecision.UNDERDETERMINED,
                "scalar.out_of_order_history",
                "A historical write cannot overwrite newer materialized branch state.",
                before=before,
                after=after,
            )

        return CausalEvaluation(
            AdmissionDecision.ADMITTED,
            "scalar.transition",
            "Ordered deterministic update is admissible.",
            before=before,
            after=after,
            delta={candidate.state_key: [before, after]},
        )
