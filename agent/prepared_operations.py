from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from .actions import ActionRegistry


@dataclass(frozen=True)
class PreparedOperation:
    """Host-owned executable intent exposed to inference only by opaque key/label."""

    key: str
    label: str
    operation_type: str
    operation_payload: Mapping[str, Any]
    side_effect_class: str
    result_mode: str
    freshness_policy: str = "contextual"
    required_authority_use: str | None = None
    source_node_id: UUID | None = None
    source_state_version: int | None = None

    def choice(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label}

    def materialization(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type,
            "operation_payload": dict(self.operation_payload),
            "side_effect_class": self.side_effect_class,
            "result_mode": self.result_mode,
            "freshness_policy": self.freshness_policy,
            "required_authority_use": self.required_authority_use,
            "source_node_id": str(self.source_node_id) if self.source_node_id else None,
            "source_state_version": self.source_state_version,
        }


class PreparedOperationBoundary:
    """Validate host-built operations before an LLM is allowed to select them."""

    def __init__(self, registry: ActionRegistry):
        self.registry = registry

    def prepare(
        self, *, key: str, label: str, operation_type: str,
        operation_payload: Mapping[str, Any], worker_class: str,
        freshness_policy: str = "contextual",
        source_node_id: UUID | None = None,
        source_state_version: int | None = None,
    ) -> PreparedOperation:
        spec = self.registry.get(operation_type)
        if spec is None:
            raise ValueError(f"unknown prepared operation {operation_type!r}")
        if spec.allowed_worker_classes and worker_class not in spec.allowed_worker_classes:
            raise PermissionError(
                f"{operation_type!r} is not available to {worker_class!r}"
            )
        # Payloads are constructed by AIOS, not by inference. Schema validation
        # still occurs at dispatch; this boundary freezes the policy metadata
        # the model must never be able to choose or rewrite.
        return PreparedOperation(
            key=str(key).strip().upper(),
            label=str(label).strip(),
            operation_type=operation_type,
            operation_payload=dict(operation_payload),
            side_effect_class=spec.side_effect_class,
            result_mode=spec.result_mode,
            freshness_policy=str(freshness_policy or "contextual"),
            required_authority_use=spec.required_authority_use,
            source_node_id=source_node_id,
            source_state_version=source_state_version,
        )
