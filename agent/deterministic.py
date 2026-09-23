from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID


DeterministicHandler = Callable[[UUID, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class DeterministicTaskSpec:
    task_type: str
    handler: DeterministicHandler
    description: str = ""


class DeterministicTaskRegistry:
    """Optional function path for repetitive cognitive work.

    AUTO mode uses a registered deterministic implementation when one exists;
    otherwise the task falls through to LLM inference. DETERMINISTIC mode
    requires a registered implementation. LLM mode always uses inference.
    """

    def __init__(self):
        self._specs: dict[str, DeterministicTaskSpec] = {}

    def register(self, spec: DeterministicTaskSpec) -> None:
        if spec.task_type in self._specs:
            raise ValueError(f"Deterministic task '{spec.task_type}' already registered")
        self._specs[spec.task_type] = spec

    def get(self, task_type: str) -> DeterministicTaskSpec | None:
        return self._specs.get(task_type)


DEFAULT_DETERMINISTIC_TASKS = DeterministicTaskRegistry()
