"""Character-agnostic source-cognition admission and diagnostics regressions."""
from aios_app.epistemic import message_cognition as cognition
from aios_app.epistemic.epistemic_scope import install_message_cognition_scope_guard


ACTOR = "Character_A"
OTHER = "Speaker_B"


def extract(text, speaker=ACTOR, *, audit=None):
    install_message_cognition_scope_guard(cognition)
    return cognition.interpret_message(
        text, character_id=ACTOR, speaker_id=speaker,
        speaker_role="character" if speaker == ACTOR else "user",
        viewpoint_id=speaker, diagnostics=audit,
    )


def test_explicit_positive_and_contracted_commitments():
    for line in ("I will repair the receiver.", "I'll repair the receiver.",
                 "I’m going to repair the receiver."):
        units = extract(line)
        goals = [u for u in units if u.claim_kind == "GOAL"]
        assert len(goals) == 1, (line, units)
        assert goals[0].polarity == 1
        assert goals[0].meta["character_owned"]
        assert goals[0].meta["intent_type"] == "commitment"
        assert "repair the receiver" in goals[0].meta["objective"]


def test_refusal_is_scene_position_not_new_actionable_goal():
    for line in ("I'm not going anywhere.", "I won't leave."):
        units = extract(line)
        assert not any(u.claim_kind == "GOAL" for u in units)
        assert any(u.claim_kind == "STATE" and u.polarity == 1
                   and u.meta["parse_reason"] == "expressed_scene_refusal"
                   for u in units)


def test_external_requests_and_private_beliefs_are_not_adopted():
    diagnostics = []
    units = extract(
        "Character_A wants to repair the receiver.", OTHER, audit=diagnostics
    )
    assert not any(u.claim_kind == "GOAL" for u in units)
    assert any(d["reason"] == "external_goal_not_adopted" for d in diagnostics)
    assert not any(u.claim_kind == "BELIEF" for u in extract(
        "Character_A believes the machine is dangerous.", OTHER
    ))


def test_discourse_marker_and_unresolved_agreement_are_explained():
    audit = []
    assert extract("You know what, that's fair.", OTHER, audit=audit) == []
    assert any(d["reason"] == "discourse_marker" for d in audit)
    audit = []
    assert extract("Deal.", audit=audit) == []
    assert any(d["reason"] == "ambiguous_intention_or_agreement" for d in audit)


def test_conditional_commitment_does_not_get_unconditional_goal():
    audit = []
    units = extract("If you help, I will repair the receiver.", audit=audit)
    assert not any(u.claim_kind == "GOAL" for u in units)
    assert any(d["reason"].startswith("nonassertive_scope:") for d in audit)


def test_unresolved_short_commitment_stays_for_bounded_enrichment():
    for phrase in ("I'll go.", "I'll learn how.", "I'll do it."):
        assert not any(u.claim_kind == "GOAL" for u in extract(phrase))
        assert cognition.ambiguous_cognition_sentences(
            phrase, character_id=ACTOR, speaker_id=ACTOR,
            speaker_role="character", viewpoint_id=ACTOR,
        )


def test_need_is_not_recast_as_future_performed_action():
    line = "I'm going to need the sofa."
    assert not any(u.claim_kind == "GOAL" for u in extract(line))
    assert cognition.ambiguous_cognition_sentences(
        line, character_id=ACTOR, speaker_id=ACTOR,
        speaker_role="character", viewpoint_id=ACTOR,
    )


def test_negative_goal_does_not_cancel_unrelated_positive_clause():
    units = extract("I don't want to announce anything, but I will repair the receiver.")
    goals = [u for u in units if u.claim_kind == "GOAL"]
    assert len(goals) == 2
    assert goals[0].polarity == -1
    assert "repair the receiver" not in goals[0].meta["objective"]
    assert goals[1].polarity == 1
    assert "repair the receiver" in goals[1].meta["objective"]
