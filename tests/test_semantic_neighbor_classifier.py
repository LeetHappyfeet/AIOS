from aios_app.semantic_index.neighbor_classifier import classify_neighbor_pair
from aios_app.semantic_index.validation_adapter import _json_object


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
        "epistemic_scope": "character",
        "character_id": "andrew",
        "character_instance_id": "instance-a",
        "viewpoint_id": "andrew",
    }
    base.update(overrides)
    return base


def test_existing_conflict_is_candidate_hint_not_proof():
    relation, _, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(),
        b=proposition(object_norm="red truck"),
        conflict_type="exclusive_object",
    )
    assert relation != "CONTRADICTS"
    assert features["legacy_conflict_type"] == "exclusive_object"
    assert features["exclusive_slot_conflict"] is False


def test_opposite_polarity_requires_same_semantic_target():
    relation, confidence, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(predicate_norm="be_definition_of", object_norm="real", polarity=1),
        b=proposition(predicate_norm="be_definition_of", object_norm="real", polarity=-1),
        conflict_type="opposite_polarity",
    )
    assert relation == "CONTRADICTS"
    assert confidence >= 0.8
    assert features["polarity_conflict"] is True

    relation, _, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(predicate_norm="be_definition_of", object_norm="real", polarity=-1),
        b=proposition(predicate_norm="be_definition_of", object_norm="digital", polarity=1),
        conflict_type="opposite_polarity",
    )
    assert relation != "CONTRADICTS"
    assert features["polarity_conflict"] is False


def test_single_value_slot_can_validate_object_conflict():
    relation, _, features = classify_neighbor_pair(
        similarity=0.93,
        a=proposition(predicate_norm="status", object_norm="online"),
        b=proposition(predicate_norm="status", object_norm="offline"),
        conflict_type="exclusive_object",
    )
    assert relation == "CONTRADICTS"
    assert features["exclusive_slot_conflict"] is True


def test_same_character_conflict_is_internal_belief_conflict():
    relation, _, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(predicate_norm="status", object_norm="open"),
        b=proposition(predicate_norm="status", object_norm="closed"),
        conflict_type="exclusive_object",
    )
    assert relation == "CONTRADICTS"
    assert features["scope_relation"] == "CHAR_SAME_VIEWPOINT"
    assert features["epistemic_interpretation"] == "internal_belief_conflict"
    assert features["objective_conflict_eligible"] is False
    assert features["character_conflict_eligible"] is True


def test_different_characters_are_viewpoint_disagreement():
    relation, _, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(predicate_norm="status", object_norm="open"),
        b=proposition(
            predicate_norm="status",
            object_norm="closed",
            character_id="beth",
            character_instance_id="instance-b",
            viewpoint_id="beth",
        ),
        conflict_type="exclusive_object",
    )
    assert relation == "CONTRADICTS"
    assert features["scope_relation"] == "CHAR_CROSS_VIEWPOINT"
    assert features["epistemic_interpretation"] == "viewpoint_disagreement"
    assert features["objective_conflict_eligible"] is False
    assert features["character_conflict_eligible"] is False


def test_character_world_conflict_is_belief_world_discrepancy():
    relation, _, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(predicate_norm="status", object_norm="open"),
        b=proposition(
            predicate_norm="status",
            object_norm="closed",
            epistemic_scope="world",
            character_id=None,
            character_instance_id=None,
            viewpoint_id=None,
        ),
        conflict_type="exclusive_object",
    )
    assert relation == "CONTRADICTS"
    assert features["scope_relation"] == "CHAR_WORLD"
    assert features["epistemic_interpretation"] == "belief_world_discrepancy"
    assert features["objective_conflict_eligible"] is False


def test_same_world_conflict_is_objective_conflict_eligible():
    world_item = {
        "epistemic_scope": "world",
        "character_id": None,
        "character_instance_id": None,
        "viewpoint_id": None,
    }
    relation, _, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(predicate_norm="status", object_norm="open", **world_item),
        b=proposition(predicate_norm="status", object_norm="closed", **world_item),
        conflict_type="exclusive_object",
    )
    assert relation == "CONTRADICTS"
    assert features["scope_relation"] == "WORLD_SAME_WORLD"
    assert features["epistemic_interpretation"] == "objective_world_conflict"
    assert features["objective_conflict_eligible"] is True


def test_different_worlds_are_divergence_not_objective_conflict():
    relation, _, features = classify_neighbor_pair(
        similarity=0.94,
        a=proposition(
            predicate_norm="status",
            object_norm="open",
            epistemic_scope="world",
            character_id=None,
            character_instance_id=None,
            viewpoint_id=None,
            world_id="world-1",
        ),
        b=proposition(
            predicate_norm="status",
            object_norm="closed",
            epistemic_scope="world",
            character_id=None,
            character_instance_id=None,
            viewpoint_id=None,
            world_id="world-2",
        ),
        conflict_type="exclusive_object",
    )
    assert relation == "CONTRADICTS"
    assert features["scope_relation"] == "WORLD_CROSS_WORLD"
    assert features["epistemic_interpretation"] == "cross_world_divergence"
    assert features["objective_conflict_eligible"] is False


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


def test_validation_adapter_accepts_asyncpg_json_text():
    assert _json_object('{"verifier_version":"v1","nested":{"status":"verified"}}') == {
        "verifier_version": "v1",
        "nested": {"status": "verified"},
    }
    assert _json_object(None) == {}
    assert _json_object('["not", "an", "object"]') == {}
