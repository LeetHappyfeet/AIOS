"""Experiment 4 source-fidelity and goal-ownership regression fixtures."""
from aios_app.epistemic.semantic_integrity import validate_frame
from aios_app.epistemic.message_cognition import interpret_message


def test_future_speech_requires_modal_content():
    result = validate_frame(
        "My mother will say 'Georgie.'",
        {"subject": "george constanza's mother", "predicate": "say",
         "object": None, "modality": "asserted"},
    )
    assert result.status == "incomplete"
    assert "future_reported_speech_not_actual_event" in result.reasons


def test_figurative_metabolism_not_certified_as_literal():
    result = validate_frame(
        "My mother can't metabolize honesty.",
        {"subject": "george constanza's mother", "predicate": "metabolize",
         "object": "honesty", "modality": "asserted"},
    )
    assert result.status == "incomplete"
    assert "figurative_literal_scope_unverified" in result.reasons


def test_adjectival_hunted_look_does_not_become_hunting_event():
    result = validate_frame(
        "George's face did the thing again — the gray, hunted look of a man.",
        {"subject": "the gray", "predicate": "hunt", "object": "look of a man"},
    )
    assert result.status == "invalid"


def _cognition(text, speaker):
    return interpret_message(text, character_id="Renamon", speaker_id=speaker,
                             speaker_role="character" if speaker == "Renamon" else "user",
                             viewpoint_id=speaker)


def test_negated_need_does_not_swallow_following_refusal():
    units = [u for u in _cognition(
        "I don't need to announce it, but I'm not going to lie to your mother.",
        "Renamon",
    ) if u.claim_kind == "GOAL"]
    assert units
    assert units[0].polarity == -1
    assert units[0].meta["objective"] == "to announce it"
    assert "mother" not in units[0].meta["objective"]


def test_positive_goal_negation_is_not_borrowed_from_later_clause():
    units = [u for u in _cognition(
        "I want to study chemistry, but I won't do it tonight.", "Renamon",
    ) if u.claim_kind == "GOAL"]
    assert units and units[0].polarity == 1
    assert units[0].meta["objective"] == "to study chemistry"


def test_george_cannot_assign_goal_and_renamon_can_adopt_one():
    assert not [u for u in _cognition(
        "Renamon needs to leave before my mother sees you.", "George Constanza",
    ) if u.claim_kind == "GOAL"]
    goals = [u for u in _cognition(
        "I want to stay in the apartment.", "Renamon",
    ) if u.claim_kind == "GOAL"]
    assert goals and goals[0].polarity == 1 and goals[0].meta["character_owned"]
