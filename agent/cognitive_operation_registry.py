from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID


@dataclass(frozen=True)
class CognitiveOperationSpec:
    name: str
    allowed_faculties: frozenset[str]
    freshness_default: str = "contextual"


SPECS = {
    "memory.retrieve": CognitiveOperationSpec(
        "memory.retrieve", frozenset({"reflection","planning","executive"})
    ),
    "corpus.search": CognitiveOperationSpec(
        "corpus.search", frozenset({"research","planning","executive"})
    ),
    "reflection.review": CognitiveOperationSpec(
        "reflection.review", frozenset({"reflection","executive"})
    ),
    "planning.review": CognitiveOperationSpec(
        "planning.review", frozenset({"planning","executive"})
    ),
    "planning.form_goal": CognitiveOperationSpec(
        "planning.form_goal", frozenset({"planning","executive"}), "strict"
    ),
    "executive.review": CognitiveOperationSpec(
        "executive.review", frozenset({"executive"}), "strict"
    ),
}


@dataclass(frozen=True)
class PreparedCognitiveOperation:
    operation_type: str
    operation_payload: Mapping[str, Any]
    faculty: str
    freshness_policy: str
    source_node_id: UUID | None
    source_state_version: int | None


class CognitiveOperationRegistry:
    """Host-owned registry for internal cognition, separate from capabilities/actions."""

    def get(self, name: str) -> CognitiveOperationSpec | None:
        return SPECS.get(name)

    def prepare(
        self, *, operation_type: str, operation_payload: Mapping[str, Any],
        faculty: str, freshness_policy: str | None = None,
        source_node_id: UUID | None = None,
        source_state_version: int | None = None,
    ) -> PreparedCognitiveOperation:
        spec=self.get(operation_type)
        if spec is None:
            raise ValueError(f"unknown cognitive operation {operation_type!r}")
        if spec.allowed_faculties and faculty not in spec.allowed_faculties:
            raise PermissionError(
                f"{operation_type!r} is not available to cognitive faculty {faculty!r}"
            )
        return PreparedCognitiveOperation(
            operation_type=spec.name,
            operation_payload=dict(operation_payload),
            faculty=faculty,
            freshness_policy=str(freshness_policy or spec.freshness_default),
            source_node_id=source_node_id,
            source_state_version=source_state_version,
        )
