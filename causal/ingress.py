from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from .kernel import CausalIntegrityKernel
from .types import CausalCandidate


DETERMINISTIC_SOURCE_KINDS = {
    "sensor",
    "simulator",
    "device",
    "external_system",
    "runtime",
    "system",
    "tool",
}


class DeterministicIngress:
    """Structured entry point for machine-authored objective state.

    This intentionally does not accept natural-language assertions.  Sensors,
    simulators and deterministic plugins supply typed values plus provenance;
    semantic confidence is not part of this contract.
    """

    def __init__(self, kernel: CausalIntegrityKernel):
        self.kernel = kernel

    @staticmethod
    def _source_kind(source_kind: str) -> str:
        value = str(source_kind).strip().lower()
        if value not in DETERMINISTIC_SOURCE_KINDS:
            raise ValueError(
                f"'{source_kind}' is not a deterministic source kind; "
                "natural-language/user/character assertions must use semantic ingestion"
            )
        return value

    async def commit_scalar(
        self,
        *,
        world_id: UUID,
        timeline_id: UUID,
        entity_id: UUID,
        state_key: str,
        value: Any,
        source_kind: str,
        source_ref: str,
        occurred_at: Optional[datetime] = None,
        unit: Optional[str] = None,
        uncertainty: Any = None,
        expected_state_version: Optional[int] = None,
        candidate_id: Optional[UUID] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        source_kind = self._source_kind(source_kind)
        envelope: Any
        if unit is not None or uncertainty is not None:
            envelope = {
                "value": value,
                "unit": unit,
                "uncertainty": uncertainty,
            }
        else:
            envelope = value

        return await self.kernel.require_commit(
            CausalCandidate(
                candidate_id=candidate_id,
                world_id=world_id,
                timeline_id=timeline_id,
                domain_id="world.scalar",
                event_type="measurement" if source_kind in {"sensor", "device"} else "set",
                entity_id=entity_id,
                state_key=state_key,
                value=envelope,
                occurred_at=occurred_at,
                source_kind=source_kind,
                source_ref=source_ref,
                authority_kind="machine_observation" if source_kind in {"sensor", "device"} else "deterministic_system",
                parameters=dict(meta or {}),
            ),
            expected_state_version=expected_state_version,
        )

    async def commit_location(
        self,
        *,
        world_id: UUID,
        timeline_id: UUID,
        entity_id: UUID,
        location_entity_id: UUID,
        source_kind: str,
        source_ref: str,
        occurred_at: Optional[datetime] = None,
        expected_state_version: Optional[int] = None,
        candidate_id: Optional[UUID] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        source_kind = self._source_kind(source_kind)
        if source_kind in {"sensor", "device"}:
            event_type = "sensor_location"
            authority_kind = "machine_observation"
        elif source_kind == "simulator":
            event_type = "simulator_location"
            authority_kind = "simulated_state"
        else:
            event_type = "set_location"
            authority_kind = "deterministic_system"

        return await self.kernel.require_commit(
            CausalCandidate(
                candidate_id=candidate_id,
                world_id=world_id,
                timeline_id=timeline_id,
                domain_id="world.location",
                event_type=event_type,
                entity_id=entity_id,
                target_entity_id=location_entity_id,
                state_key="location_entity_id",
                value=str(location_entity_id),
                occurred_at=occurred_at,
                source_kind=source_kind,
                source_ref=source_ref,
                authority_kind=authority_kind,
                parameters=dict(meta or {}),
            ),
            expected_state_version=expected_state_version,
        )
