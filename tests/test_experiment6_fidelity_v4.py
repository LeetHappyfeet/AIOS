"""Character-agnostic source-coverage regressions from an episodic claim corpus.

Integrity of the represented assertion is separate from truth of testimony:
ambiguous or dropped arguments must not become unconditional valid receipts.
"""
import pytest
from aios_app.epistemic.semantic_integrity import validate_frame, revision_key, INTEGRITY_VERSION


@pytest.mark.parametrize("source,frame,expected,reason", [
    ("I'd just prefer to know tonight.",
     {"subject":"Character_A","predicate":"prefer","object":None},
     "incomplete","intention_or_request_argument_lost"),
    ("I'm staying two nights.",
     {"subject":"Character_A","predicate":"stay","object":None},
     "incomplete","temporal_argument_lost"),
    ("She is right there.",
     {"subject":"she","predicate":"be","object":None},
     "incomplete","locative_or_temporal_argument_lost"),
    ("My father will sigh.",
     {"subject":"the speaker's father","predicate":"sigh","object":None,
      "modality":"asserted"},
     "incomplete","future_or_intended_modality_lost"),
    ("If she sees you, she's gonna ask questions.",
     {"subject":"she","predicate":"go","object":None,
      "modality":"asserted"},
     "invalid","future_auxiliary_misread_as_action"),
    ("He could not stop talking.",
     {"subject":"he","predicate":"stop","object":None,"modality":"asserted"},
     "incomplete","ability_or_possibility_modality_lost"),
    ("The booking fell through.",
     {"subject":"the booking","predicate":"fall","object":None},
     "incomplete","figurative_or_incomplete_fall_through"),
    ("You don't mention the message.",
     {"subject":"Character_A","predicate":"mention","object":"the message",
      "polarity":-1,"modality":"asserted"},
     "incomplete","directive_or_proposal_as_observed_event"),
    ("I'm staying for 2 nights.",
     {"subject":"Character_A","predicate":"stay","object":None},
     "incomplete","temporal_argument_lost"),
])
def test_unqualified_frame_does_not_certify_lost_source(source,frame,expected,reason):
    result = validate_frame(source,frame,speaker_id="Character_A")
    assert result.status == expected, (source,result)
    assert reason in result.reasons


def test_explicitly_retained_future_and_duration_are_not_rejected_for_loss():
    future=validate_frame("The speaker will return tomorrow.",
        {"subject":"the speaker","predicate":"return","object":"tomorrow",
         "modality":"future"})
    assert "future_or_intended_modality_lost" not in future.reasons
    duration=validate_frame("I am staying two nights.",
        {"subject":"Character_A","predicate":"stay","object":"two nights",
         "modality":"asserted"},speaker_id="Character_A")
    assert "temporal_argument_lost" not in duration.reasons


def test_integrity_version_invalidates_older_receipts():
    assert INTEGRITY_VERSION == "semantic-integrity-v4-source-coverage"
    claim={"subject":"Character_A","predicate":"stay","object":None}
    assert revision_key("I'm staying two nights.",[claim],
        version="semantic-integrity-v3-fidelity") != revision_key(
        "I'm staying two nights.",[claim],version=INTEGRITY_VERSION)
