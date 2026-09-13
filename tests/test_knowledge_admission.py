from uuid import UUID

from aios_app.epistemic.knowledge import _context_acquisition_eligible_values


INSTANCE = UUID("bba60009-c433-4ce7-9135-3499467ba0e2")
OTHER_INSTANCE = UUID("13c786d1-dc58-44d4-8d83-b4f1edbed86c")


def eligible(**overrides):
    values = {
        "instance_id": INSTANCE,
        "epistemic_scope": "character",
        "character_instance_id": INSTANCE,
        "claim_kind": "BELIEF",
        "raw_text": "Shego knew she was inside a computer.",
        "discourse_mode": "character_mental_state",
    }
    values.update(overrides)
    return _context_acquisition_eligible_values(**values)


def test_character_belief_for_exact_instance_is_admitted():
    assert eligible() is True


def test_narrative_reclassification_retracts_character_knowledge():
    assert eligible(epistemic_scope="narrative") is False


def test_wrong_runtime_instance_is_not_admitted():
    assert eligible(character_instance_id=OTHER_INSTANCE) is False


def test_event_fragments_are_not_settled_character_knowledge():
    assert eligible(
        claim_kind="EVENT",
        raw_text="She jabbed a thumb at her own chest.",
        discourse_mode="character_speech",
    ) is False


def test_question_is_not_observed_character_knowledge():
    assert eligible(
        claim_kind="STATE",
        raw_text="Some tech company's secret weapon?",
        discourse_mode="character_speech",
    ) is False


def test_quoted_question_with_closing_quote_is_rejected():
    assert eligible(
        claim_kind="BELIEF",
        raw_text='"Am I trapped?"',
        discourse_mode="character_mental_state",
    ) is False


def test_nonassertive_discourse_is_not_settled_knowledge():
    assert eligible(
        claim_kind="BELIEF",
        raw_text="Shego might be trapped.",
        discourse_mode="hypothetical",
    ) is False


def test_memory_goal_rule_state_trait_relationship_remain_eligible():
    for claim_kind in (
        "MEMORY",
        "GOAL",
        "RULE",
        "STATE",
        "TRAIT",
        "RELATIONSHIP",
    ):
        assert eligible(claim_kind=claim_kind) is True
