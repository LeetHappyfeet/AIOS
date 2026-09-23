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
