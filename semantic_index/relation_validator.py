from __future__ import annotations

from typing import Any

from aios_app.epistemic.hypothesis_validation import evaluate_matrix

RELATION_VERIFIER_VERSION = "semantic-relation-matrix-v4-scope"

# These predicates represent narrow, single-valued semantic slots closely
# enough that two different positive values can be treated as competitors.
# Grammatical catch-alls such as ``be`` and ``be_definition_of`` are
# deliberately excluded: differing complements are not contradictions by
# themselves.
STRICT_SINGLE_VALUE_PREDICATES = {
    "identity",
    "located_at",
    "location",
    "born_in",
    "status",
}


def _contains_refinement(a: str | None, b: str | None) -> bool:
    """Return true when one normalized object phrase strictly contains the other."""
    if not a or not b:
        return False
    left = a.strip().lower()
    right = b.strip().lower()
    if left == right:
        return False
    return left in right or right in left


def _memory_scope(item: dict[str, Any]) -> str:
    """Return the epistemic memory domain without inferring truth from world_id.

    world_id is carried on character memories because a character exists in a
    world.  It therefore cannot be used on its own to decide that a proposition
    belongs to /world memory.
    """
    explicit = str(item.get("epistemic_scope") or "").strip().lower()
    if explicit == "character":
        return "character"
    if explicit == "world":
        return "world"
    if item.get("character_instance_id") or item.get("character_id"):
        return "character"
    return "unknown"


def classify_scope_relation(a: dict[str, Any], b: dict[str, Any]) -> str:
    """Classify how two proposition memories relate epistemically.

    This is intentionally independent of the semantic relation.  A semantic
    contradiction across two characters is disagreement, not a world-level
    contradiction; a contradiction between /char and /world is a belief/world
    discrepancy; and different worlds may legitimately diverge.
    """
    a_scope = _memory_scope(a)
    b_scope = _memory_scope(b)

    if a_scope == "world" and b_scope == "world":
        a_world = a.get("world_id")
        b_world = b.get("world_id")
        if a_world and b_world and a_world == b_world:
            return "WORLD_SAME_WORLD"
        return "WORLD_CROSS_WORLD"

    if a_scope == "character" and b_scope == "character":
        a_instance = a.get("character_instance_id")
        b_instance = b.get("character_instance_id")
        a_viewpoint = a.get("viewpoint_id")
        b_viewpoint = b.get("viewpoint_id")
        same_instance = bool(a_instance and b_instance and a_instance == b_instance)
        same_viewpoint = bool(a_viewpoint and b_viewpoint and a_viewpoint == b_viewpoint)
        if same_instance or same_viewpoint:
            return "CHAR_SAME_VIEWPOINT"
        return "CHAR_CROSS_VIEWPOINT"

    if {a_scope, b_scope} == {"character", "world"}:
        return "CHAR_WORLD"

    return "UNSCOPED"


def _epistemic_interpretation(relation: str, scope_relation: str) -> str:
    if relation != "CONTRADICTS":
        return "consistent_or_related"
    return {
        "WORLD_SAME_WORLD": "objective_world_conflict",
        "WORLD_CROSS_WORLD": "cross_world_divergence",
        "CHAR_SAME_VIEWPOINT": "internal_belief_conflict",
        "CHAR_CROSS_VIEWPOINT": "viewpoint_disagreement",
        "CHAR_WORLD": "belief_world_discrepancy",
    }.get(scope_relation, "semantic_conflict_unscoped")


def validate_neighbor_relation(
    *,
    similarity: float,
    a: dict[str, Any],
    b: dict[str, Any],
    conflict_type: str | None,
) -> tuple[str, float, dict[str, Any]]:
    same_subject = bool(a.get("subject_norm") and a.get("subject_norm") == b.get("subject_norm"))
    same_predicate = bool(a.get("predicate_norm") and a.get("predicate_norm") == b.get("predicate_norm"))
    same_object = bool(a.get("object_norm") and a.get("object_norm") == b.get("object_norm"))
    same_polarity = int(a.get("polarity") or 1) == int(b.get("polarity") or 1)
    same_topic = bool(a.get("topic_key") and a.get("topic_key") == b.get("topic_key"))
    same_timeline = bool(a.get("timeline_id") and a.get("timeline_id") == b.get("timeline_id"))
    same_world = bool(a.get("world_id") and a.get("world_id") == b.get("world_id"))
    worlds_known = bool(a.get("world_id") and b.get("world_id"))
    both_events = a.get("claim_kind") == "EVENT" and b.get("claim_kind") == "EVENT"
    scope_relation = classify_scope_relation(a, b)

    object_refinement = bool(
        same_subject
        and same_predicate
        and same_polarity
        and _contains_refinement(a.get("object_norm"), b.get("object_norm"))
    )

    # A durable proposition_conflict row is a reason to inspect a pair, not
    # evidence that proves the pair contradictory.  Contradiction is rebuilt
    # from independent semantic evidence here.
    polarity_conflict = bool(
        same_subject
        and same_predicate
        and same_object
        and not same_polarity
    )
    predicate = str(a.get("predicate_norm") or "").strip().lower()
    exclusive_slot_conflict = bool(
        same_subject
        and same_predicate
        and same_polarity
        and a.get("object_norm")
        and b.get("object_norm")
        and not same_object
        and predicate in STRICT_SINGLE_VALUE_PREDICATES
    )
    semantic_conflict = polarity_conflict or exclusive_slot_conflict
    conflict_penalty = -2 if semantic_conflict else 0

    matrix = {
        "EQUIVALENT": {
            "subject": 2 if same_subject else -2,
            "predicate": 2 if same_predicate else -2,
            "object": 2 if same_object else -1,
            "polarity": 2 if same_polarity else -3,
            "vector": 2 if similarity >= 0.90 else (1 if similarity >= 0.82 else -1),
            "semantic_conflict": conflict_penalty,
        },
        "REFINES": {
            "subject": 2 if same_subject else -2,
            "predicate": 2 if same_predicate else -1,
            "object": 2 if object_refinement else (1 if not same_object else 0),
            "polarity": 2 if same_polarity else -3,
            "vector": 1 if similarity >= 0.78 else -1,
            "semantic_conflict": conflict_penalty,
        },
        "SAME_EVENT": {
            "event": 3 if both_events else -3,
            "timeline": 3 if same_timeline else -3,
            "world": 2 if same_world else (-3 if worlds_known else 0),
            "subject": 1 if same_subject else 0,
            "vector": 2 if similarity >= 0.86 else (1 if similarity >= 0.80 else -2),
            "semantic_conflict": conflict_penalty,
        },
        "SAME_TOPIC": {
            "topic": 3 if same_topic else -2,
            "world": 1 if same_world else 0,
            "vector": 2 if similarity >= 0.82 else (1 if similarity >= 0.76 else -1),
            "semantic_conflict": conflict_penalty,
        },
        "CONTRADICTS": {
            "subject": 2 if same_subject else -3,
            "predicate": 2 if same_predicate else -3,
            # Semantic incompatibility must stand on its own. Scope only
            # changes what the incompatibility means epistemically.
            "target": 4 if semantic_conflict else -3,
            "scope": 1 if scope_relation in {"WORLD_SAME_WORLD", "CHAR_SAME_VIEWPOINT"} else 0,
            # Existing conflict rows are intentionally neutral evidence.
            "legacy_conflict": 0,
        },
        "RELATED": {
            "vector": 3 if similarity >= 0.86 else (2 if similarity >= 0.76 else 0),
            "topic": 1 if same_topic else 0,
            "subject": 1 if same_subject else 0,
            "world": 1 if same_world else 0,
            "semantic_conflict": conflict_penalty,
        },
    }

    if semantic_conflict:
        proposed = "CONTRADICTS"
    elif same_subject and same_predicate and same_object and same_polarity:
        proposed = "EQUIVALENT"
    elif object_refinement:
        proposed = "REFINES"
    elif both_events and same_timeline and similarity >= 0.80:
        proposed = "SAME_EVENT"
    elif same_topic:
        proposed = "SAME_TOPIC"
    else:
        proposed = "RELATED"

    outcome = evaluate_matrix(
        matrix,
        proposed_key=proposed,
        min_score=4,
        min_margin=2,
        min_stability=0.60,
    )

    relation = outcome.winner_key if outcome.status == "verified" and outcome.winner_key else "UNRESOLVED"
    confidence = 0.0
    if relation != "UNRESOLVED":
        evidence_ratio = max(0.0, min(1.0, (outcome.winner_score + 6) / 16))
        confidence = min(0.98, 0.55 * float(similarity) + 0.45 * evidence_ratio)

    interpretation = _epistemic_interpretation(relation, scope_relation)
    features = {
        "similarity": round(float(similarity), 6),
        "same_subject": same_subject,
        "same_predicate": same_predicate,
        "same_object": same_object,
        "object_refinement": object_refinement,
        "same_polarity": same_polarity,
        "same_topic": same_topic,
        "same_timeline": same_timeline,
        "same_world": same_world,
        "both_events": both_events,
        "legacy_conflict_type": conflict_type,
        "polarity_conflict": polarity_conflict,
        "exclusive_slot_conflict": exclusive_slot_conflict,
        "scope_relation": scope_relation,
        "epistemic_interpretation": interpretation,
        "objective_conflict_eligible": bool(
            relation == "CONTRADICTS" and scope_relation == "WORLD_SAME_WORLD"
        ),
        "character_conflict_eligible": bool(
            relation == "CONTRADICTS" and scope_relation == "CHAR_SAME_VIEWPOINT"
        ),
        "adversarial_verification": outcome.as_meta(),
        "verifier_version": RELATION_VERIFIER_VERSION,
    }
    return relation, confidence, features
