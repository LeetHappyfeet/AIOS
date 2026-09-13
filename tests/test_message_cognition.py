from aios_app.epistemic.message_cognition import (
    INTERPRETER_VERSION,
    MAX_UNITS,
    interpret_message,
)


def _interpret(text: str, *, speaker: str = "Ren-119", viewpoint: str = "Shego_001"):
    return interpret_message(
        text,
        character_id="Shego_001",
        speaker_id=speaker,
        speaker_role="user" if speaker != "Shego_001" else "assistant",
        viewpoint_id=viewpoint,
    )


def test_message_cognition_is_bounded_and_salient():
    text = " ".join(
        [
            "Shego looked around.",
            "The wallpaper was beige.",
            "Shego knows she is digital data inside a computer.",
            "Shego remembers being trapped in a comic book.",
            "Ren offers to help her if she works as his battle buddy.",
            "Shego wants to escape confinement.",
            "Shego is uncertain whether she ever physically existed.",
        ]
        + [f"She moved her hand {i}." for i in range(40)]
    )
    units = _interpret(text)
    assert len(units) <= MAX_UNITS
    rendered = " ".join(unit.text.lower() for unit in units)
    assert "digital data" in rendered
    assert "remembers" in rendered
    assert "wants to escape" in rendered
    assert "uncertain" in rendered
    assert "wallpaper" not in rendered


def test_message_cognition_classifies_core_kinds():
    units = _interpret(
        "Shego remembers the show ending. "
        "Shego wants to escape. "
        "Shego believes she is alive."
    )
    kinds = {unit.claim_kind for unit in units}
    assert {"MEMORY", "GOAL", "BELIEF"}.issubset(kinds)


def test_negation_changes_polarity():
    units = _interpret("Shego believes she is not trapped.")
    assert units
    assert units[0].polarity == -1


def test_other_speaker_first_person_desire_is_not_character_goal():
    units = _interpret("I want to leave this place.", speaker="Ren-119")
    assert not [unit for unit in units if unit.claim_kind == "GOAL"]


def test_character_first_person_desire_is_character_goal():
    units = _interpret(
        "I want to leave this place.",
        speaker="Shego_001",
        viewpoint="Shego_001",
    )
    goals = [unit for unit in units if unit.claim_kind == "GOAL"]
    assert len(goals) == 1
    assert goals[0].meta["character_owned"] is True
    assert goals[0].meta["semantic_owner"] == "Shego_001"


def test_second_person_question_is_not_character_goal():
    units = _interpret("You want me to be your assistant?")
    assert not [unit for unit in units if unit.claim_kind == "GOAL"]


def test_question_about_desire_is_not_character_goal():
    units = _interpret("What do you want?")
    assert not [unit for unit in units if unit.claim_kind == "GOAL"]


def test_causal_impulse_is_not_durable_goal():
    units = _interpret("It made her want to scream.")
    assert not [unit for unit in units if unit.claim_kind == "GOAL"]


def test_explicit_narrated_character_goal_is_kept():
    units = _interpret("Shego prepared to tear her way out of the system.")
    goals = [unit for unit in units if unit.claim_kind == "GOAL"]
    assert len(goals) == 1
    assert goals[0].meta["persistence"] == "session"


def test_other_speaker_private_belief_is_not_character_belief():
    units = _interpret("I think this whole system is fake.", speaker="Ren-119")
    assert not [unit for unit in units if unit.claim_kind == "BELIEF"]


def test_relationship_observation_can_cross_speaker_boundary():
    units = _interpret("Ren offers to help her if she works as his battle buddy.")
    relationships = [unit for unit in units if unit.claim_kind == "RELATIONSHIP"]
    assert len(relationships) == 1
    assert relationships[0].meta["character_owned"] is False


def test_v2_interpreter_version_forces_old_commit_refresh():
    assert INTERPRETER_VERSION == "message-cognition-v2"
