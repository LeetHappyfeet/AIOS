from aios_app.epistemic.epistemic_scope import (
    SCOPE_CONDITIONAL,
    SCOPE_HYPOTHETICAL,
    classify_scope,
    split_strong_clauses,
)
from aios_app.epistemic.message_cognition import INTERPRETER_VERSION, interpret_message
from aios_app.epistemic.semantic_frames import decompose_sentence


def _cognition(text: str):
    return interpret_message(
        text,
        character_id="Shego_001",
        speaker_id="Shego_001",
        speaker_role="character",
        viewpoint_id="Shego_001",
    )


def test_scope_classifier_marks_supposition_and_condition():
    assert classify_scope("Let's say I believe you.") == SCOPE_HYPOTHETICAL
    assert classify_scope("If Ren is telling the truth, I'll help him.") == SCOPE_CONDITIONAL


def test_fast_cognition_does_not_promote_hypothetical_belief():
    assert INTERPRETER_VERSION == "message-cognition-v4"
    units = _cognition("Let's say I believe you.")
    assert not any(unit.claim_kind == "BELIEF" for unit in units)


def test_fast_cognition_does_not_promote_conditional_goal():
    units = _cognition("If Ren is telling the truth, I want to help him.")
    assert not any(unit.claim_kind == "GOAL" for unit in units)


def test_strong_dash_separates_independent_clause():
    parts = split_strong_clauses("And before you start pitching—I'm not your assistant.")
    assert parts == ["And before you start pitching", "I'm not your assistant."]


def test_fast_cognition_does_not_use_discourse_marker_as_subject():
    units = _cognition("And before you start pitching—I'm not your assistant.")
    relationship = next(unit for unit in units if unit.claim_kind == "RELATIONSHIP")
    assert not relationship.text.lower().startswith("and:")
    assert relationship.meta["semantic_owner"] == "Shego_001"


def test_semantic_frames_inherit_hypothetical_scope():
    frames = decompose_sentence("Let's say I believe you.")
    assert frames
    assert all(frame.modality == SCOPE_HYPOTHETICAL for frame in frames)
    assert any((frame.predicate_canonical or "").lower() == "believe" for frame in frames)


def test_semantic_frames_split_dash_before_subject_resolution():
    frames = decompose_sentence("And before you start pitching—I'm not your assistant.")
    assert len(frames) >= 2
    subjects = {(frame.subject or "").lower() for frame in frames}
    assert "i" in subjects
    assert "and" not in subjects
