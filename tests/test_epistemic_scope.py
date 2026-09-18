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


def test_semantic_frames_do_not_link_across_sentence_boundary():
    text = (
        'Did you rehearse that in the hallway?" '
        "She pulls up a window on the screen—a log file, timestamps in neat little rows, "
        "and slides it across the desktop like she's flicking a card across a table."
    )
    frames = decompose_sentence(text)
    assert frames
    by_index = {frame.index: frame for frame in frames}

    rehearse = next(frame for frame in frames if frame.predicate_canonical == "rehearse")
    assert rehearse.object_frame_index is None

    for frame in frames:
        if frame.parent_index is not None:
            assert frame.parent_index in by_index
        if frame.object_frame_index is not None:
            assert frame.object_frame_index in by_index

    assert not any(
        "log file" in (frame.subject or "").lower()
        for frame in frames
    )


def test_semantic_frames_preserve_same_sentence_nested_clauses():
    frames = decompose_sentence(
        "Mia spent the whole night feeding me log files and setting three alarms I didn't ask for."
    )
    predicates = {(frame.predicate_canonical or "").lower() for frame in frames}
    assert {"spend", "feed", "set", "ask"} <= predicates
    assert any(frame.parent_index is not None for frame in frames)
