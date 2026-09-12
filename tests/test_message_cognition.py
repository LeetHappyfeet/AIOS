from aios_app.epistemic.message_cognition import MAX_UNITS, interpret_message


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
    units = interpret_message(
        text,
        character_id="Shego_001",
        speaker_id="Ren-119",
        speaker_role="user",
        viewpoint_id="Shego_001",
    )
    assert len(units) <= MAX_UNITS
    rendered = " ".join(unit.text.lower() for unit in units)
    assert "digital data" in rendered
    assert "remembers" in rendered
    assert "wants to escape" in rendered
    assert "uncertain" in rendered


def test_message_cognition_classifies_core_kinds():
    units = interpret_message(
        "Shego remembers the show ending. Shego wants to escape. Shego believes she is alive.",
        character_id="Shego_001",
        speaker_id="Ren-119",
        speaker_role="user",
        viewpoint_id="Shego_001",
    )
    kinds = {unit.claim_kind for unit in units}
    assert {"MEMORY", "GOAL", "BELIEF"}.issubset(kinds)


def test_negation_changes_polarity():
    units = interpret_message(
        "Shego believes she is not trapped.",
        character_id="Shego_001",
        speaker_id="Ren-119",
        speaker_role="user",
        viewpoint_id="Shego_001",
    )
    assert units
    assert units[0].polarity == -1
