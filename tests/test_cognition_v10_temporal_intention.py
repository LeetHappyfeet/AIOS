"""Generic, source-owned V10 intention and negative admission regressions."""
from aios_app.epistemic import message_cognition as cognition


def _units(text, *, speaker="Character_A"):
    return cognition.interpret_message(
        text, character_id="Character_A", speaker_id=speaker,
        speaker_role="character" if speaker=="Character_A" else "user",
        viewpoint_id=speaker,
    )


def test_time_bounded_progressive_is_actionable_character_commitment():
    units=_units("I'm staying two nights.")
    goals=[u for u in units if u.claim_kind=="GOAL"]
    assert len(goals)==1
    assert goals[0].polarity==1 and goals[0].meta["character_owned"]
    assert goals[0].meta["intent_type"]=="commitment"
    assert goals[0].meta["objective"]=="stay two nights"


def test_unbounded_progressive_is_not_invented_plan():
    assert not any(u.claim_kind=="GOAL" for u in _units("I'm looking out the window."))


def test_information_preference_is_queued_for_bounded_review_not_fabricated():
    text="I'd just prefer to know tonight."
    assert not [u for u in _units(text) if u.claim_kind=="GOAL"]
    review=cognition.ambiguous_cognition_sentences(
        text,character_id="Character_A",speaker_id="Character_A",
        speaker_role="character",viewpoint_id="Character_A")
    assert review and "prefer" in review[0].lower()


def test_external_speaker_cannot_assign_a_time_bounded_goal():
    assert not [u for u in _units("I'm staying two nights.",speaker="Speaker_B")
                if u.claim_kind=="GOAL"]


def test_loaded_runtime_version_is_composite_v10():
    assert cognition.INTERPRETER_VERSION==(
        "message-cognition-v10-temporal-intent+epistemic-scope-v1")
