from __future__ import annotations

from typing import Sequence

from ..types import (
    AdmissionDecision,
    CausalCandidate,
    CausalEvaluation,
    CausalState,
)


class LocationDomain:
    """Exclusive physical location for one entity on one timeline.

    This domain deliberately does not assume real-world travel speeds.  AIOS
    core enforces exclusivity and transition continuity; worlds may add travel,
    teleportation, portals, or other mechanisms as rules/domains later.
    """

    domain_id = "world.location"
    _explicit_transitions = {"move", "enter", "leave", "transport", "teleport", "set_location"}
    _authoritative_observations = {"sensor_location", "simulator_location"}

    def evaluate(
        self,
        state: CausalState | None,
        candidate: CausalCandidate,
        *,
        rules: Sequence[dict],
    ) -> CausalEvaluation:
        destination = candidate.value
        if destination is None:
            return CausalEvaluation(
                AdmissionDecision.REJECTED_IMPOSSIBLE,
                "location.missing_destination",
                "A location transition requires a destination.",
                before=state.value if state else None,
            )

        before = state.value if state else None
        if state is None:
            return CausalEvaluation(
                AdmissionDecision.ADMITTED,
                "location.initial",
                "Initial branch location is admissible.",
                before=None,
                after=destination,
                delta={"location": [None, destination]},
            )

        if before == destination:
            return CausalEvaluation(
                AdmissionDecision.ADMITTED,
                "location.idempotent",
                "Location agrees with the canonical branch state.",
                before=before,
                after=destination,
                delta={},
            )

        # Two incompatible endpoint assertions at the same causal coordinate are
        # a paradox unless the candidate itself is an explicit transition.
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

        if candidate.event_type in self._explicit_transitions:
            return CausalEvaluation(
                AdmissionDecision.ADMITTED,
                "location.explicit_transition",
                "An explicit location transition connects the two states.",
                before=before,
                after=destination,
                delta={"location": [before, destination]},
            )

        if candidate.event_type in self._authoritative_observations:
            if same_node or same_time:
                return CausalEvaluation(
                    AdmissionDecision.CONFLICT,
                    "location.concurrent_authoritative_conflict",
                    "Two authoritative location measurements disagree at the same causal coordinate.",
                    before=before,
                    after=destination,
                )
            return CausalEvaluation(
                AdmissionDecision.ADMITTED_WITH_LATENT_TRANSITION,
                "location.authoritative_endpoint_transition",
                "Authoritative endpoints changed; the unobserved transition remains unspecified.",
                before=before,
                after=destination,
                delta={"location": [before, destination]},
                latent_events=({
                    "domain_id": self.domain_id,
                    "event_type": "latent_movement",
                    "from": before,
                    "to": destination,
                    "mechanism": "unknown",
                },),
            )

        if same_node or same_time:
            return CausalEvaluation(
                AdmissionDecision.REJECTED_IMPOSSIBLE,
                "location.exclusive_coordinate",
                "One physical entity cannot occupy two exclusive locations at the same branch coordinate.",
                before=before,
                after=destination,
            )

        if bool(candidate.parameters.get("allow_latent_transition")):
            if (
                candidate.occurred_at
                and state.occurred_at
                and candidate.occurred_at < state.occurred_at
            ):
                return CausalEvaluation(
                    AdmissionDecision.UNDERDETERMINED,
                    "location.out_of_order_history",
                    "The candidate predates the current materialized state and requires historical reconstruction.",
                    before=before,
                    after=destination,
                )
            return CausalEvaluation(
                AdmissionDecision.ADMITTED_WITH_LATENT_TRANSITION,
                "location.latent_transition",
                "The endpoint is causally possible only with an omitted movement transition.",
                before=before,
                after=destination,
                delta={"location": [before, destination]},
                latent_events=({
                    "domain_id": self.domain_id,
                    "event_type": "latent_movement",
                    "from": before,
                    "to": destination,
                    "mechanism": "unknown",
                },),
            )

        return CausalEvaluation(
            AdmissionDecision.CONFLICT,
            "location.transition_required",
            "The proposed location conflicts with canonical state and provides no admissible transition.",
            before=before,
            after=destination,
        )
