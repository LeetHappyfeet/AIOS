from aios_app.epistemic.event_projection import project_semantic_event


def _member(pid, subject, predicate, obj, text, score=1.0, vector=None):
    relevance = {"total": score}
    if vector is not None:
        relevance["vector_similarity"] = vector
    return {
        "proposition_id": pid,
        "subject_norm": subject,
        "predicate_norm": predicate,
        "object_norm": obj,
        "text": text,
        "relevance": relevance,
    }


def test_event_projection_prefers_action_over_low_information_state():
    event = {"semantic_event_id": "e1", "event_confidence": 0.9}
    projection = project_semantic_event(
        event,
        [
            _member("p1", "it", "look", "closed", "It looks closed.", score=2.0),
            _member("p2", "renamon", "inspect", "door", "Renamon inspected the door.", score=1.5),
            _member("p3", "door", "be", "closed", "The door was closed.", score=1.4),
        ],
    )
    assert projection["primary_proposition_id"] == "p2"
    assert projection["text"].startswith("renamon inspect door.")
    assert "door was closed." in projection["text"]
    assert projection["projection_version"] == "semantic-event-projection-v1"


def test_event_projection_keeps_best_vector_evidence_without_propagation():
    projection = project_semantic_event(
        {"semantic_event_id": "e1", "event_confidence": 0.7},
        [
            _member("p1", "renamon", "inspect", "door", "Renamon inspected the door.", vector=0.82),
            _member("p2", "door", "be", "closed", "The door was closed."),
        ],
    )
    assert projection["best_vector_similarity"] == 0.82
    assert projection["member_proposition_ids"] == ["p1", "p2"]


def test_event_projection_falls_back_to_richest_member_text():
    projection = project_semantic_event(
        {"semantic_event_id": "e1", "event_confidence": 0.5},
        [_member("p1", "it", "be", "_", "Something happened in the market.")],
    )
    assert projection["text"] == "Something happened in the market."
