from aios_app.epistemic.message_cognition import ambiguous_cognition_sentences
from aios_app.epistemic.message_cognition_enrichment import (
    _effective_goal_horizon,
    _specific_commitment_objective,
)


def test_explicit_goal_stays_on_deterministic_fast_path():
    text="I want to learn chemistry."
    assert ambiguous_cognition_sentences(
        text,character_id="Renamon",speaker_id="Renamon",
        speaker_role="character",viewpoint_id="Renamon")==[]


def test_explicit_future_goal_gets_temporal_adjudication_without_reparsing():
    text="I want to visit the bookstore tomorrow."
    assert ambiguous_cognition_sentences(
        text,character_id="Renamon",speaker_id="Renamon",
        speaker_role="character",viewpoint_id="Renamon")==[text]


def test_implicit_commitment_is_offered_for_bounded_enrichment():
    text='"Fine. I\'ll learn how," Renamon said.'
    rows=ambiguous_cognition_sentences(
        text,character_id="Renamon",speaker_id="Renamon",
        speaker_role="character",viewpoint_id="Renamon")
    assert rows and "I'll learn how" in rows[0]


def test_other_speaker_never_classifies_character_intention():
    text="You should learn chemistry."
    assert ambiguous_cognition_sentences(
        text,character_id="Renamon",speaker_id="Mia",
        speaker_role="user",viewpoint_id="Mia")==[]


def test_future_timing_is_kept_with_short_character_commitment():
    rows=ambiguous_cognition_sentences(
        'Tomorrow afternoon. "I\'ll go."',character_id="Renamon",
        speaker_id="Renamon",speaker_role="character",viewpoint_id="Renamon")
    assert rows == ['Tomorrow afternoon. "I\'ll go."']


def test_future_commitment_cannot_be_downgraded_to_immediate():
    assert _effective_goal_horizon(
        "immediate", "commitment", "Tomorrow afternoon. I'll go.") == "session"
    assert _effective_goal_horizon(
        "immediate", "plan", "Tomorrow afternoon. I'll return.") == "session"
    assert _effective_goal_horizon(
        "immediate", "desire", "I want to visit tomorrow.") == "session"
    assert _effective_goal_horizon(
        "immediate", "immediate_intention", "I am going now.") == "immediate"


def test_commitment_requires_a_specific_enough_objective():
    assert not _specific_commitment_objective("go", "commitment")
    assert _specific_commitment_objective("go to bookstore", "commitment")



def test_george_request_does_not_become_renamon_goal():
    from aios_app.epistemic.message_cognition import interpret_message
    units = interpret_message(
        "But you need to leave before my mother sees you.",
        character_id="Renamon", speaker_id="George Constanza",
        speaker_role="user", viewpoint_id="George Constanza",
    )
    assert not any(unit.claim_kind == "GOAL" for unit in units)


def test_external_named_goal_is_not_adopted():
    from aios_app.epistemic.message_cognition import interpret_message
    units = interpret_message(
        "Renamon needs to leave.",
        character_id="Renamon", speaker_id="George Constanza",
        speaker_role="user", viewpoint_id="George Constanza",
    )
    assert not any(unit.claim_kind == "GOAL" for unit in units)


def test_self_authored_goal_remains_admissible():
    from aios_app.epistemic.message_cognition import interpret_message
    units = interpret_message(
        "I want to learn chemistry.",
        character_id="Renamon", speaker_id="Renamon",
        speaker_role="character", viewpoint_id="Renamon",
    )
    assert any(unit.claim_kind == "GOAL" and unit.meta["character_owned"] for unit in units)
