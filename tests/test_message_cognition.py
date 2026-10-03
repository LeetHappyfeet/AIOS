from aios_app.epistemic import message_cognition as cognition
from aios_app.epistemic.message_cognition import question_semantics


def _interpret(text: str, *, speaker: str = "Shego_001", viewpoint: str = "Shego_001"):
    return cognition.interpret_message(
        text,
        character_id="Shego_001",
        speaker_id=speaker,
        speaker_role="assistant",
        viewpoint_id=viewpoint,
    )


def test_first_person_goal_owned_by_character():
    units = _interpret("I want to get out of this computer.")
    goals = [unit for unit in units if unit.claim_kind == "GOAL"]
    assert len(goals) == 1
    assert goals[0].meta["character_owned"] is True
    assert goals[0].meta["semantic_owner"] == "Shego_001"
    assert goals[0].text == "Shego wants to get out of this computer."
    assert goals[0].meta["intent_type"] == "desire"
    assert goals[0].meta["horizon"] == "session"
    assert goals[0].meta["objective"] == "to get out of this computer"


def test_second_person_other_speaker_is_not_character_authored():
    units = _interpret("You are digital now.", speaker="Ren-119", viewpoint="Ren-119")
    states = [unit for unit in units if unit.claim_kind == "STATE"]
    assert len(states) == 1
    assert states[0].meta["character_owned"] is False


def test_question_does_not_become_goal():
    units = _interpret("Do you want to be my battle buddy?")
    assert not [unit for unit in units if unit.claim_kind == "GOAL"]


def test_causal_desire_does_not_become_goal():
    units = _interpret("The pressure made her want to escape.")
    assert not [unit for unit in units if unit.claim_kind == "GOAL"]


def test_memory_is_durable():
    units = _interpret("She remembers the show ending.")
    memories = [unit for unit in units if unit.claim_kind == "MEMORY"]
    assert len(memories) == 1
    assert memories[0].meta["persistence"] == "durable"


def test_belief_is_until_contradicted():
    units = _interpret("She believes this system is a prison.")
    beliefs = [unit for unit in units if unit.claim_kind == "BELIEF"]
    assert len(beliefs) == 1
    assert beliefs[0].meta["persistence"] == "until_contradicted"


def test_explicit_rule_is_until_contradicted():
    units = _interpret("Shego must not reveal the password.")
    rules = [unit for unit in units if unit.claim_kind == "RULE"]
    assert len(rules) == 1
    assert rules[0].meta["persistence"] == "until_contradicted"


def test_state_is_until_changed():
    units = _interpret("She is digital now.")
    states = [unit for unit in units if unit.claim_kind == "STATE"]
    assert len(states) == 1
    assert states[0].meta["persistence"] == "until_changed"


def test_completed_event_is_turn_scoped():
    units = _interpret("She escaped the room.")
    events = [unit for unit in units if unit.claim_kind == "EVENT"]
    assert len(events) == 1
    assert events[0].meta["persistence"] == "turn"


def test_prepared_goal_is_session_scoped():
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


def test_effective_interpreter_version_includes_scope_policy():
    from aios_app.epistemic.epistemic_scope import install_message_cognition_scope_guard
    install_message_cognition_scope_guard(cognition)
    assert cognition.INTERPRETER_VERSION == (
        "message-cognition-v9-candidate-admission+epistemic-scope-v1"
    )


def test_event_does_not_promote_first_token_to_subject():
    units = _interpret("Beneath its usual registers, something changed.")
    events = [unit for unit in units if unit.claim_kind == "EVENT"]
    assert len(events) == 1
    assert events[0].meta["semantic_owner"] is None
    assert events[0].meta["character_owned"] is False



def test_question_semantics_extracts_indirect_character_preference():
    result = question_semantics(
        (
            'Mia looks confused. "But what is it about sleeves in a fashion sense '
            'that interests you so much? If someone helped you out by giving you '
            'some character and style out the gate it seems like they must have cared."'
        ),
        character_id="Renamon",
        speaker_id="Mia",
    )
    assert result is not None
    assert result.subject == "Renamon"
    assert result.relation == "preference"
    assert "sleeves" in result.topic_terms
    assert result.confidence >= 0.90


def test_question_semantics_does_not_turn_external_fact_question_into_self_memory():
    result = question_semantics(
        "What is hematite?",
        character_id="Renamon",
        speaker_id="Mia",
    )
    assert result is None


def test_question_semantics_remains_nonpersistent():
    text = "What is it about sleeves that interests you so much?"
    assert cognition.interpret_message(
        text,
        character_id="Renamon",
        speaker_id="Mia",
        speaker_role="user",
        viewpoint_id="Renamon",
    ) == []



def test_question_semantics_distinguishes_wear_history_from_preference():
    history = question_semantics(
        "Have you ever worn sleeves before?",
        character_id="Renamon",
        speaker_id="Mia",
    )
    preference = question_semantics(
        "What would you wear if you picked for yourself?",
        character_id="Renamon",
        speaker_id="Mia",
    )
    assert history is not None
    assert history.relation == "experience"
    assert preference is not None
    assert preference.relation == "preference"
