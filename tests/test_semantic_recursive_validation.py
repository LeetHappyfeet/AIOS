from aios_app.epistemic.hypothesis_validation import evaluate_matrix
from aios_app.semantic_index.relation_validator import validate_neighbor_relation


def test_matrix_rejects_fragile_winner_when_leave_one_axis_changes_result():
    matrix = {
        "A": {"identity": 3, "vector": 2, "world": -1},
        "B": {"identity": 0, "vector": 2, "world": 2},
    }
    outcome = evaluate_matrix(
        matrix,
        proposed_key="A",
        min_score=3,
        min_margin=2,
        min_stability=0.75,
    )
    assert outcome.status == "fragile"
    assert outcome.winner_key == "A"
    assert outcome.stability < 0.75


def test_matrix_accepts_candidate_supported_by_independent_axes():
    matrix = {
        "character:Renamon": {"scope": 3, "identity": 3, "world": 1, "vector": 2},
        "world:w": {"scope": -2, "identity": 0, "world": 2, "vector": -1},
    }
    outcome = evaluate_matrix(matrix, proposed_key="character:Renamon")
    assert outcome.status == "verified"
    assert outcome.margin >= 3
    assert outcome.stability >= 0.75


def test_same_event_requires_timeline_and_world_consistency():
    relation, _, features = validate_neighbor_relation(
        similarity=0.94,
        a={
            "subject_norm": "renamon",
            "predicate_norm": "open",
            "object_norm": "door",
            "polarity": 1,
            "topic_key": "door/open",
            "claim_kind": "EVENT",
            "timeline_id": "t1",
            "world_id": "w1",
        },
        b={
            "subject_norm": "renamon",
            "predicate_norm": "open",
            "object_norm": "door",
            "polarity": 1,
            "topic_key": "door/open",
            "claim_kind": "EVENT",
            "timeline_id": "t2",
            "world_id": "w2",
        },
        conflict_type=None,
    )
    assert relation != "SAME_EVENT"
    assert features["adversarial_verification"]["matrix"]["SAME_EVENT"]["timeline"] == -3
    assert features["adversarial_verification"]["matrix"]["SAME_EVENT"]["world"] == -3


def test_same_event_can_survive_when_independent_event_evidence_agrees():
    relation, confidence, features = validate_neighbor_relation(
        similarity=0.91,
        a={
            "subject_norm": "renamon",
            "predicate_norm": "open",
            "object_norm": "door",
            "polarity": 1,
            "topic_key": "door/open",
            "claim_kind": "EVENT",
            "timeline_id": "t1",
            "world_id": "w1",
        },
        b={
            "subject_norm": "renamon",
            "predicate_norm": "swing open",
            "object_norm": "door",
            "polarity": 1,
            "topic_key": "door/open",
            "claim_kind": "EVENT",
            "timeline_id": "t1",
            "world_id": "w1",
        },
        conflict_type=None,
    )
    assert relation == "SAME_EVENT"
    assert confidence > 0.5
    assert features["adversarial_verification"]["status"] == "verified"


def test_explicit_conflict_can_defeat_similarity_equivalence():
    relation, _, features = validate_neighbor_relation(
        similarity=0.97,
        a={
            "subject_norm": "door",
            "predicate_norm": "state",
            "object_norm": "locked",
            "polarity": 1,
            "topic_key": "door/state",
            "claim_kind": "STATE",
            "timeline_id": "t1",
            "world_id": "w1",
        },
        b={
            "subject_norm": "door",
            "predicate_norm": "state",
            "object_norm": "unlocked",
            "polarity": -1,
            "topic_key": "door/state",
            "claim_kind": "STATE",
            "timeline_id": "t1",
            "world_id": "w1",
        },
        conflict_type="opposite_polarity",
    )
    assert relation == "CONTRADICTS"
    assert features["adversarial_verification"]["winner_key"] == "CONTRADICTS"
