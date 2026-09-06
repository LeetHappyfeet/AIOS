from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from aios_app.hud.context import HUDContext


_WORD_RE = re.compile(r"[a-z0-9_'-]+")


def _words(value: Any) -> set[str]:
    return set(_WORD_RE.findall(str(value or "").lower()))


def _overlap(a: Iterable[str], b: Iterable[str]) -> float:
    left, right = set(a), set(b)
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RelevanceBreakdown:
    recency: float = 0.0
    semantic: float = 0.0
    entity_proximity: float = 0.0
    goal: float = 0.0
    relationship: float = 0.0
    emotional_salience: float = 0.0
    memory_salience: float = 0.0
    confidence: float = 0.0
    causal_proximity: float = 0.0
    branch_penalty: float = 0.0
    epistemic_penalty: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.recency
            + self.semantic
            + self.entity_proximity
            + self.goal
            + self.relationship
            + self.emotional_salience
            + self.memory_salience
            + self.confidence
            + self.causal_proximity
            - self.branch_penalty
            - self.epistemic_penalty
        )

    def as_dict(self) -> dict[str, float]:
        return {**asdict(self), "total": round(self.total, 6)}


class HUDRelevanceScorer:
    """Deterministic, inspectable relevance scoring for HUD candidates."""

    def __init__(self, context: HUDContext, *, focus_text: str = "", goals: Iterable[Any] = ()):
        self.context = context
        self.focus_words = _words(focus_text)
        self.goal_words = _words(" ".join(str(goal) for goal in goals))

    def score(
        self,
        candidate: Mapping[str, Any],
        *,
        rank: int = 0,
        candidate_text: str = "",
        candidate_world_id: Optional[UUID] = None,
        candidate_entity_id: Optional[UUID] = None,
        epistemic_status: Optional[str] = None,
        confidence: Optional[float] = None,
        updated_at: Optional[datetime] = None,
        causal_distance: Optional[int] = None,
    ) -> RelevanceBreakdown:
        text_words = _words(candidate_text)
        now = datetime.now(timezone.utc)

        if updated_at is not None:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (now - updated_at).total_seconds() / 3600.0)
            recency = 1.4 / (1.0 + math.log1p(age_hours))
        else:
            recency = 1.0 / (1.0 + max(0, rank))

        semantic = 1.8 * _overlap(text_words, self.focus_words)
        goal = 1.3 * _overlap(text_words, self.goal_words)
        entity_proximity = 1.4 if self.context.entity_is_active(candidate_entity_id) else 0.0

        relationship = 0.0
        if candidate.get("relationship_type") or candidate.get("trust") is not None:
            relationship = 0.8
            if candidate_entity_id and self.context.entity_is_active(candidate_entity_id):
                relationship += 0.5

        emotional_salience = min(
            1.0,
            max(
                _as_float(candidate.get("emotional_salience")),
                abs(_as_float(candidate.get("affinity"))),
            ),
        )
        memory_salience = min(
            1.2,
            max(
                _as_float(candidate.get("salience_weight")),
                _as_float(candidate.get("attention_weight")),
            ),
        )
        confidence_score = 0.8 * max(
            0.0,
            min(1.0, _as_float(confidence, _as_float(candidate.get("effective_confidence"), 0.5))),
        )

        causal_proximity = 0.0
        if causal_distance is not None:
            causal_proximity = 1.2 / (1.0 + max(0, causal_distance))

        branch_penalty = 0.0 if self.context.world_visible(candidate_world_id) else 100.0
        epistemic_penalty = 0.0
        if (epistemic_status or "").lower() in {"rejected", "superseded", "disbelieved"}:
            epistemic_penalty = 2.0

        return RelevanceBreakdown(
            recency=recency,
            semantic=semantic,
            entity_proximity=entity_proximity,
            goal=goal,
            relationship=relationship,
            emotional_salience=emotional_salience,
            memory_salience=memory_salience,
            confidence=confidence_score,
            causal_proximity=causal_proximity,
            branch_penalty=branch_penalty,
            epistemic_penalty=epistemic_penalty,
        )
