"""Regression: planning model cannot invent authoritative intentions."""
from aios_app.epistemic.goal_formation_admission import verify_formed_goal
from aios_app.epistemic.goal_source_admission import review_goal_source


def check(source, proposal, speaker="Renamon", role="character"):
    return verify_formed_goal(
        source_text=source, proposal=proposal,
        character_id="Renamon", speaker_id=speaker, speaker_role=role,
    )


def test_direct_author_owned_goal_selects_original_objective():
    result = check("I want to visit the thrift store.", "Renamon wants to visit the thrift store.")
    assert result.eligible
    assert result.unit.meta["objective"] == "to visit the thrift store"


def test_overlap_cannot_append_new_goal_content():
    result = check("I want to visit the thrift store.",
                   "Renamon wants to visit the thrift store and steal money.")
    assert not result.eligible


def test_foreign_speaker_cannot_author_character_intention():
    assert not check("I want to visit the thrift store.",
                     "Renamon wants to visit the thrift store.", speaker="Alex").eligible
    assert not check("I want to visit the thrift store.",
                     "Renamon wants to visit the thrift store.", role="user").eligible


def test_hypothetical_cannot_be_promoted():
    assert review_goal_source(source_text="Perhaps I want to visit the store.",
                              objective="to visit the store",
                              match_span=(8, len("Perhaps I want to visit the store."))).decision != "admit"
    assert not check("Perhaps I want to visit the store.",
                     "Renamon wants to visit the store.").eligible


def test_negative_desire_is_not_formed_as_positive_goal():
    assert not check("I don't want to visit the thrift store.",
                     "Renamon wants to visit the thrift store.").eligible


def test_nonliteral_first_person_cannot_form_goal():
    assert not check("Her gesture suggested I will visit the thrift store.",
                     "Renamon intends to visit the thrift store.").eligible
