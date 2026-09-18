from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CognitiveRetrievalPolicy:
    """Generation-time retrieval policy owned by cognition, not HUD presentation."""

    memory_hops: int = 2
    belief_hops: int = 2
    event_hops: int = 2
    goal_hops: int = 1
    rule_hops: int = 1
    semantic_retrieval_limit: int = 25
    deep_memory_limit: int = 0
    recent_context_limit: int = 12

    @property
    def memory_limit(self) -> int:
        return min(250, self.semantic_retrieval_limit + max(0, self.deep_memory_limit))

    @property
    def effective_memory_hops(self) -> int:
        return max(self.memory_hops, 3 if self.deep_memory_limit > 0 else 2)


DEFAULT_COGNITIVE_RETRIEVAL_POLICY = CognitiveRetrievalPolicy()
