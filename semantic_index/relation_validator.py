from __future__ import annotations

from typing import Any

from aios_app.epistemic.hypothesis_validation import evaluate_matrix

RELATION_VERIFIER_VERSION = "semantic-relation-matrix-v2"


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
    has_conflict = bool(conflict_type)

    # semantic_neighbor_relation is a single primary relation.  An already
    # materialized proposition conflict is therefore negative evidence for the
    # mutually-exclusive non-conflict labels.  This does not make the conflict
    # record infallible: it still has to survive the matrix and leave-one-axis
    # stability test against subject/predicate/object/world evidence.
    conflict_veto = -3 if has_conflict else 0

    matrix = {
        "EQUIVALENT": {
            "subject": 2 if same_subject else -2,
            "predicate": 2 if same_predicate else -2,
            "object": 2 if same_object else -1,
            "polarity": 2 if same_polarity else -3,
            "vector": 2 if similarity >= 0.90 else (1 if similarity >= 0.82 else -1),
            "conflict": conflict_veto,
        },
        "REFINES": {
            "subject": 2 if same_subject else -2,
            "predicate": 2 if same_predicate else -1,
            "object": 1 if not same_object else 0,
            "polarity": 2 if same_polarity else -3,
            "vector": 1 if similarity >= 0.78 else -1,
            "conflict": conflict_veto,
        },
        "SAME_EVENT": {
            "event": 3 if both_events else -3,
            "timeline": 3 if same_timeline else -3,
            "world": 2 if same_world else (-3 if worlds_known else 0),
            "subject": 1 if same_subject else 0,
            "vector": 2 if similarity >= 0.86 else (1 if similarity >= 0.80 else -2),
            "conflict": conflict_veto,
        },
        "SAME_TOPIC": {
            "topic": 3 if same_topic else -2,
            "world": 1 if same_world else 0,
            "vector": 2 if similarity >= 0.82 else (1 if similarity >= 0.76 else -1),
            "conflict": conflict_veto,
        },
        "CONTRADICTS": {
            "conflict": 3 if has_conflict else -3,
            "subject": 1 if same_subject else 0,
            "predicate": 1 if same_predicate else 0,
            # Exclusive-object conflicts are supported by the same canonical
            # subject/predicate pointing at incompatible objects.  Polarity
            # disagreement is a separate contradiction signal.
            "object": 2 if (has_conflict and same_subject and same_predicate and not same_object) else 0,
            "polarity": 2 if not same_polarity else 0,
            "world": 1 if same_world else (-2 if worlds_known else 0),
        },
        "RELATED": {
            "vector": 3 if similarity >= 0.86 else (2 if similarity >= 0.76 else 0),
            "topic": 1 if same_topic else 0,
            "subject": 1 if same_subject else 0,
            "world": 1 if same_world else 0,
            "conflict": conflict_veto,
        },
    }

    if conflict_type:
        proposed = "CONTRADICTS"
    elif same_subject and same_predicate and same_object and same_polarity:
        proposed = "EQUIVALENT"
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

    features = {
        "similarity": round(float(similarity), 6),
        "same_subject": same_subject,
        "same_predicate": same_predicate,
        "same_object": same_object,
        "same_polarity": same_polarity,
        "same_topic": same_topic,
        "same_timeline": same_timeline,
        "same_world": same_world,
        "both_events": both_events,
        "conflict_type": conflict_type,
        "adversarial_verification": outcome.as_meta(),
        "verifier_version": RELATION_VERIFIER_VERSION,
    }
    return relation, confidence, features
