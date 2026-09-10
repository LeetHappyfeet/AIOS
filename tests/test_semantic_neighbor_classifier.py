from aios_app.semantic_index.neighbor_classifier import classify_neighbor_pair


def proposition(**overrides):
    base = {
        "topic_key": "topic-a",
        "subject_norm": "andrew",
        "predicate_norm": "owns",
        "object_norm": "blue truck",
        "polarity": 1,
        "claim_kind": "BELIEF",
        "predicate_family": "possession",
        "world_id": "world-1",
        "timeline_id": "timeline-1",
    }
    base.update(overrides)
    return base


def test_existing_conflict_dominates_pairwise_relation():
    relation, confidence, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(),
        b=proposition(object_norm="red truck"),
        conflict_type="exclusive_object",
    )
    assert relation == "CONTRADICTS"
    assert confidence >= 0.9
    assert features["conflict_type"] == "exclusive_object"


def test_object_containment_is_refinement_when_subject_predicate_match():
    relation, confidence, _ = classify_neighbor_pair(
        similarity=0.90,
        a=proposition(object_norm="truck"),
        b=proposition(object_norm="blue pickup truck"),
        conflict_type=None,
    )
    assert relation == "REFINES"
    assert confidence >= 0.8


def test_event_neighbors_on_same_timeline_can_be_same_event():
    relation, confidence, _ = classify_neighbor_pair(
        similarity=0.88,
        a=proposition(
            topic_key="event-a",
            predicate_norm="arrived_at",
            object_norm="warehouse",
            claim_kind="EVENT",
            predicate_family="action",
        ),
        b=proposition(
            topic_key="event-b",
            predicate_norm="entered",
            object_norm="warehouse",
            claim_kind="EVENT",
            predicate_family="action",
        ),
        conflict_type=None,
    )
    assert relation == "SAME_EVENT"
    assert confidence >= 0.8
