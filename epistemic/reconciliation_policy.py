from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


POLICY_ENGINE_VERSION = "semantic-policy-v1"


@dataclass(frozen=True)
class ReconciliationPolicy:
    name: str
    mode: str
    accept_support: float = 0.60
    decision_margin: float = 0.15
    exclusive_slot: bool = False


@dataclass(frozen=True)
class EvidencePoint:
    polarity: int
    weight: float
    order: float = 0.0
    correlation_key: str = ""


DEFAULT_POLICY = ReconciliationPolicy("generic", "accumulate")

FAMILY_POLICIES = {
    # Durable facts can accumulate independent corroboration.
    "IDENTITY": ReconciliationPolicy("identity", "accumulate", 0.60, 0.15),
    "SOCIAL": ReconciliationPolicy("relationship", "accumulate", 0.60, 0.15),
    "MEMBERSHIP": ReconciliationPolicy("membership", "accumulate", 0.60, 0.15),
    "POSSESSION": ReconciliationPolicy("possession", "accumulate", 0.60, 0.15),
    "EPISTEMIC": ReconciliationPolicy("epistemic", "accumulate", 0.55, 0.12),
    "MEMORY": ReconciliationPolicy("memory", "accumulate", 0.55, 0.12),
    "CAUSAL": ReconciliationPolicy("causal", "accumulate", 0.65, 0.15),
    "COMMUNICATION": ReconciliationPolicy("communication", "accumulate", 0.60, 0.15),
    "ACTION": ReconciliationPolicy("event", "accumulate", 0.60, 0.15),
    "TEMPORAL": ReconciliationPolicy("temporal", "accumulate", 0.60, 0.15),
    # Volatile state should follow the newest admitted state rather than grow
    # increasingly certain merely because old observations remain in history.
    "DESCRIPTIVE": ReconciliationPolicy("descriptive_state", "latest", 0.50, 0.05),
    "EMOTIONAL": ReconciliationPolicy("emotional_state", "latest", 0.50, 0.05),
    "GOAL": ReconciliationPolicy("goal_lifecycle", "latest", 0.50, 0.05),
    # Location is both volatile and normally single-valued for an entity at a
    # single timeline coordinate. The SQL character policy enforces the slot.
    "SPATIAL": ReconciliationPolicy("location_state", "latest", 0.50, 0.05, exclusive_slot=True),
    # Repeated low-authority copies of a rule must not bootstrap one another
    # into certainty. A rule therefore uses strongest admitted evidence.
    "RULE": ReconciliationPolicy("authority_rule", "max", 0.70, 0.15),
}


def policy_for_family(predicate_family: str | None) -> ReconciliationPolicy:
    return FAMILY_POLICIES.get((predicate_family or "UNKNOWN").upper(), DEFAULT_POLICY)


def _clamp(value: float) -> float:
    return max(0.0, min(0.999999, float(value)))


def aggregate_support(
    evidence: Iterable[EvidencePoint],
    policy: ReconciliationPolicy,
) -> tuple[float, float, int]:
    """Aggregate already-admitted evidence according to a semantic policy.

    Correlation keys are collapsed before aggregation so repeated extraction of
    one source/event coordinate cannot strengthen a belief. The caller decides
    how source/event identity is formed.
    """

    points = list(evidence)
    if not points:
        return 0.0, 0.0, 0

    correlated: dict[tuple[int, str], EvidencePoint] = {}
    for point in points:
        key = (1 if point.polarity >= 0 else -1, point.correlation_key or f"row:{id(point)}")
        current = correlated.get(key)
        if current is None or _clamp(point.weight) > _clamp(current.weight):
            correlated[key] = point

    values = list(correlated.values())
    if policy.mode == "latest":
        latest = max(values, key=lambda p: (p.order, _clamp(p.weight), p.correlation_key))
        weight = _clamp(latest.weight)
        return (weight, 0.0, 1) if latest.polarity >= 0 else (0.0, weight, 1)

    if policy.mode == "max":
        positive = max((_clamp(p.weight) for p in values if p.polarity >= 0), default=0.0)
        negative = max((_clamp(p.weight) for p in values if p.polarity < 0), default=0.0)
        return positive, negative, len(values)

    positive_product = 1.0
    negative_product = 1.0
    positive_seen = False
    negative_seen = False
    for point in values:
        weight = _clamp(point.weight)
        if point.polarity >= 0:
            positive_product *= 1.0 - weight
            positive_seen = True
        else:
            negative_product *= 1.0 - weight
            negative_seen = True

    positive = 1.0 - positive_product if positive_seen else 0.0
    negative = 1.0 - negative_product if negative_seen else 0.0
    return positive, negative, len(values)


def choose_stance(
    positive_support: float,
    negative_support: float,
    policy: ReconciliationPolicy,
) -> tuple[str, float]:
    positive = max(0.0, min(1.0, float(positive_support)))
    negative = max(0.0, min(1.0, float(negative_support)))
    if positive >= policy.accept_support and positive - negative >= policy.decision_margin:
        stance = "positive"
    elif negative >= policy.accept_support and negative - positive >= policy.decision_margin:
        stance = "negative"
    else:
        stance = "unresolved"
    return stance, abs(positive - negative)
