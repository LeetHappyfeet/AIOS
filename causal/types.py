from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID


class AdmissionDecision(str, Enum):
    """Causal result, intentionally independent of semantic confidence."""

    ADMITTED = "ADMITTED"
    ADMITTED_WITH_LATENT_TRANSITION = "ADMITTED_WITH_LATENT_TRANSITION"
    REJECTED_IMPOSSIBLE = "REJECTED_IMPOSSIBLE"
    CONFLICT = "CONFLICT"
    UNDERDETERMINED = "UNDERDETERMINED"
    FORK_REQUIRED = "FORK_REQUIRED"
    EPISTEMIC_ONLY = "EPISTEMIC_ONLY"

    @property
    def committable(self) -> bool:
        return self in {
            AdmissionDecision.ADMITTED,
            AdmissionDecision.ADMITTED_WITH_LATENT_TRANSITION,
        }


@dataclass(frozen=True)
class CausalCoordinate:
    world_id: UUID
    timeline_id: UUID
    dag_node_id: Optional[UUID] = None


@dataclass(frozen=True)
class CausalCandidate:
    """Typed proposed objective transition.

    A candidate is not truth.  Natural-language interpretation may create one,
    but only a successful causal admission can change canonical /world state.
    """

    world_id: UUID
    timeline_id: UUID
    domain_id: str
    event_type: str
    entity_id: UUID
    state_key: str
    value: Any
    dag_node_id: Optional[UUID] = None
    target_entity_id: Optional[UUID] = None
    occurred_at: Optional[datetime] = None
    source_kind: str = "semantic"
    source_ref: Optional[str] = None
    authority_kind: str = "semantic_candidate"
    parameters: dict[str, Any] = field(default_factory=dict)
    claim_id: Optional[UUID] = None
    frame_id: Optional[UUID] = None
    proposition_id: Optional[UUID] = None
    candidate_id: Optional[UUID] = None


@dataclass(frozen=True)
class CausalState:
    value: Any
    version: int
    last_event_id: Optional[UUID] = None
    last_node_id: Optional[UUID] = None
    occurred_at: Optional[datetime] = None


@dataclass(frozen=True)
class CausalEvaluation:
    decision: AdmissionDecision
    reason_code: str
    reason: str
    before: Any = None
    after: Any = None
    delta: Any = None
    latent_events: tuple[dict[str, Any], ...] = ()

    @property
    def committable(self) -> bool:
        return self.decision.committable
