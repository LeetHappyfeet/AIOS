"""Character agency persistence primitives.

This package owns durable cognitive work and action lifecycle state. It does not
own inference, action capability policy, or external side effects.
"""

from .lifecycle import (
    ActionRecord,
    CognitiveTask,
    CharacterAgencyStore,
    InvalidLifecycleTransition,
)

__all__ = [
    "ActionRecord",
    "CognitiveTask",
    "CharacterAgencyStore",
    "InvalidLifecycleTransition",
]

from .actions import ActionDispatcher, ActionRegistry, ActionSpec, default_action_registry
from .runtime import AgentRuntimeStore
from .worker import CharacterWorker

from .autonomy import AutonomyScheduler
from .deterministic import DEFAULT_DETERMINISTIC_TASKS, DeterministicTaskRegistry, DeterministicTaskSpec

from .gateway import ExternalGateway
from .policy import ActionPolicyService
