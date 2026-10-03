"""V11 source-authority and managed-intention separation, generic fixtures."""
import asyncio
from uuid import uuid4

import pytest

from aios_app.epistemic import message_cognition
from aios_app.epistemic.goal_source_admission import (
    ADMISSION_VERSION, review_goal_source,
)
from aios_app.epistemic.goals import CharacterGoalService


def units(text, diagnostics=None):
    return message_cognition.interpret_message(
        text,character_id="Character_A",speaker_id="Character_A",
        speaker_role="character",viewpoint_id="Character_A",
        diagnostics=diagnostics,
    )


def goals(text, diagnostics=None):
    return [unit for unit in units(text, diagnostics) if unit.claim_kind=="GOAL"]


def test_explicit_two_night_arrangement_is_session_commitment():
    result = goals('"I\'m staying two nights."')
    assert len(result)==1
    assert result[0].meta["objective"]=="stay two nights"
    assert result[0].meta["horizon"]=="session"
    assert result[0].meta["goal_admission_version"]==ADMISSION_VERSION
    assert result[0].meta["source_span"] is not None


@pytest.mark.parametrize("source", [
    "a small deliberate relocation that said *I will be putting this down properly in a minute.*",
    "Her gesture suggested *I will put this down properly in a minute.*",
    'She imagined saying, "I will stay until Friday."',
    '"I will stay until Friday," she imagined saying.',
])
def test_embedded_narrative_first_person_is_not_managed(source):
    diagnostics=[]
    assert not goals(source, diagnostics)
    assert any("nonliteral_statement" in item["reason"]
               for item in diagnostics)


def test_direct_future_speech_is_still_admissible():
    assert len(goals('"I will stay until Friday."'))==1


@pytest.mark.parametrize("source,objective", [
    ('"I am going to start collecting them."',"start collecting them"),
    ('"I will handle it."',"handle it"),
    ('"I will move this."',"move this"),
])
def test_unresolved_references_remain_auditable_not_active(source,objective):
    decision=review_goal_source(source_text=source,objective=objective)
    assert decision.decision=="unresolved_reference"
    diagnostics=[]
    if "handle it" not in source:
        assert not goals(source, diagnostics)
        assert any("unresolved_reference" in entry["reason"] for entry in diagnostics)


def test_direct_immediate_bag_action_is_scene_only():
    diagnostics=[]
    assert not goals('"I will put my bag down in a minute."', diagnostics)
    assert any("scene_only" in entry["reason"] for entry in diagnostics)


def test_source_attribution_does_not_depend_on_message_owner():
    diagnostics=[]
    assert not goals("The look on her face that said I will be moving tomorrow.",diagnostics)
    assert any("nonliteral_statement" in entry["reason"] for entry in diagnostics)


def test_canonical_rendering_is_not_used_as_source():
    decision=review_goal_source(
        source_text="Her gesture that said I will put this down.",
        objective="put this down",
        match_span=(len("Her gesture that said "), len("Her gesture that said I will put this down")),
    )
    assert decision.decision=="nonliteral_statement"


def test_shared_writer_blocks_source_ineligible_goal_before_any_query():
    class NeverDB:
        async def fetch(self,*args,**kwargs):
            raise AssertionError("Ineligible candidate reached goal database")
    result=asyncio.run(CharacterGoalService(NeverDB()).reconcile_evidence(
        instance_id=uuid4(), text="Character A intends to start collecting them.",
        topic_key="goal:test:collect",polarity=1,source_node_id=uuid4(),
        objective="start collecting them",
        source_text="I'm going to start collecting them.",
        parse_reason="explicit_self_commitment",horizon="session",
    ))
    assert result is None


def test_shared_writer_blocks_old_historical_narrator_goal():
    class NeverDB:
        async def fetch(self,*args,**kwargs):
            raise AssertionError("Ineligible historical goal reached goal database")
    source="a deliberate relocation that said I will be putting this down in a minute."
    result=asyncio.run(CharacterGoalService(NeverDB()).reconcile_evidence(
        instance_id=uuid4(), text="Character A intends to put this down.",
        topic_key="goal:test:bag",polarity=1,source_node_id=uuid4(),
        objective="be putting this down in a minute",
        source_text=source,parse_reason="explicit_self_commitment",horizon="session",
        source_span=[33,72],
    ))
    assert result is None
