from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from aios_app.hud.context import HUDContext


_WORD_RE = re.compile(r"[a-z0-9_'-]+")
_LOW_INFORMATION_SUBJECTS = {
    "it", "this", "that", "which", "who", "what", "something", "anything",
    "everything", "nothing", "one", "someone", "somebody",
}


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
class CognitiveRelevanceBreakdown:
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
            self.recency + self.semantic + self.entity_proximity + self.goal
            + self.relationship + self.emotional_salience + self.memory_salience
            + self.confidence + self.causal_proximity
            - self.branch_penalty - self.epistemic_penalty
        )

    def as_dict(self) -> dict[str, float]:
        return {**asdict(self), "total": round(self.total, 6)}


class CognitiveRelevanceScorer:
    """Cognition-owned relevance scoring; HUD presentation must not choose memories."""

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
    ) -> CognitiveRelevanceBreakdown:
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
            relationship = 0.8 + (
                0.5 if candidate_entity_id and self.context.entity_is_active(candidate_entity_id) else 0.0
            )
        emotional_salience = min(
            1.0,
            max(_as_float(candidate.get("emotional_salience")), abs(_as_float(candidate.get("affinity")))),
        )
        memory_salience = min(
            1.2,
            max(_as_float(candidate.get("salience_weight")), _as_float(candidate.get("attention_weight"))),
        )
        confidence_score = 0.8 * max(
            0.0,
            min(1.0, _as_float(confidence, _as_float(candidate.get("effective_confidence"), 0.5))),
        )
        causal_proximity = 0.0 if causal_distance is None else 1.2 / (1.0 + max(0, causal_distance))
        branch_penalty = 0.0 if self.context.world_visible(candidate_world_id) else 100.0
        epistemic_penalty = (
            2.0 if (epistemic_status or "").lower() in {"rejected", "superseded", "disbelieved"} else 0.0
        )
        return CognitiveRelevanceBreakdown(
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


def _candidate_tokens(item: Mapping[str, Any]) -> set[str]:
    return _words(
        " ".join(
            str(item.get(key) or "")
            for key in ("topic_key", "subject_norm", "predicate_norm", "object_norm", "text")
        )
    )


def _quality_penalty(item: Mapping[str, Any]) -> float:
    subject = str(item.get("subject_norm") or "").strip().lower()
    predicate = str(item.get("predicate_norm") or "").strip().lower()
    obj = str(item.get("object_norm") or "").strip().lower()
    penalty = 0.0
    if subject in _LOW_INFORMATION_SUBJECTS:
        penalty += 0.8
    if predicate in {"be", "identity", "have"} and (subject in _LOW_INFORMATION_SUBJECTS or len(_words(obj)) <= 1):
        penalty += 0.5
    if obj in {"", "_", "*"}:
        penalty += 1.0
    return penalty


def select_recalled_cognition(
    items: Iterable[dict[str, Any]],
    *,
    focus_text: str,
    max_items: int = 12,
    relevance_floor: float = 1.35,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Global recall competition across retrieval modes.

    Retrieval may be broad. This function decides which established memories
    become generation-time cognition. Current-message commits bypass competition.
    """
    focus_words = _words(focus_text)
    by_id: dict[Any, dict[str, Any]] = {}
    for raw in items:
        item = dict(raw)
        key = item.get("proposition_id") or item.get("text")
        existing = by_id.get(key)
        if existing is None or float(item.get("relevance", {}).get("total", 0.0)) > float(
            existing.get("relevance", {}).get("total", 0.0)
        ):
            by_id[key] = item

    immediate = [item for item in by_id.values() if item.get("cognitive_commit")]
    established = [item for item in by_id.values() if not item.get("cognitive_commit")]
    scored: list[tuple[float, dict[str, Any], set[str]]] = []
    suppressed = {"below_recall_floor": 0, "redundant_recall": 0, "recall_cap": 0}

    for item in established:
        relevance = dict(item.get("relevance") or {})
        base = float(relevance.get("total") or 0.0)
        tokens = _candidate_tokens(item)
        semantic_overlap = _overlap(tokens, focus_words)
        salience = max(
            _as_float(item.get("salience_weight")),
            _as_float(item.get("attention_weight")),
            _as_float(item.get("emotional_salience")),
        )
        confidence = _as_float(item.get("effective_confidence"), _as_float(item.get("confidence"), 0.5))
        kind = str(item.get("claim_kind") or "BELIEF").upper()
        instance_depth = int((item.get("topology") or {}).get("instance_depth") or item.get("instance_depth") or 0)
        continuity_penalty = min(0.9, math.log1p(max(0, instance_depth)) * 0.16)
        context_bonus = 1.35 * semantic_overlap
        salience_bonus = 0.45 * max(0.0, min(1.0, salience))
        confidence_bonus = 0.25 * max(0.0, min(1.0, confidence))
        kind_bonus = 0.25 if kind in {"GOAL", "RULE", "RELATIONSHIP"} else 0.0
        recall_score = base + context_bonus + salience_bonus + confidence_bonus + kind_bonus
        recall_score -= continuity_penalty + _quality_penalty(item)
        relevance["recall"] = round(recall_score, 6)
        relevance["context_overlap"] = round(semantic_overlap, 6)
        item["relevance"] = relevance
        if recall_score < relevance_floor:
            suppressed["below_recall_floor"] += 1
            continue
        scored.append((recall_score, item, tokens))

    scored.sort(key=lambda entry: entry[0], reverse=True)
    selected: list[dict[str, Any]] = []
    selected_tokens: list[set[str]] = []
    for _, item, tokens in scored:
        if len(selected) >= max_items:
            suppressed["recall_cap"] += 1
            continue
        if tokens and any(_overlap(tokens, prior) >= 0.86 for prior in selected_tokens):
            suppressed["redundant_recall"] += 1
            continue
        selected.append(item)
        selected_tokens.append(tokens)

    immediate.sort(key=lambda item: float(item.get("relevance", {}).get("total", 0.0)), reverse=True)
    return immediate + selected, suppressed
