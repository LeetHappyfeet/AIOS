from aios_app.epistemic.message_cognition import ambiguous_cognition_sentences


def test_explicit_goal_stays_on_deterministic_fast_path():
    text="I want to learn chemistry."
    assert ambiguous_cognition_sentences(
        text,character_id="Renamon",speaker_id="Renamon",
        speaker_role="character",viewpoint_id="Renamon")==[]


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
