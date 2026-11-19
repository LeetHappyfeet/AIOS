from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aios_app.epistemic.goal_source_admission import non_goal_reason, review_goal_source
from aios_app.epistemic.goals import valid_goal_objective
from aios_app.hud.frame import HUDAssembler


@pytest.mark.parametrize("objective", [
    "keep my own impressions out of the findings column",
    "Renamon intends to take that as meaning the failure was legible this time",
    "want that recorded as a limitation with the file named, not silently dropped",
    "Renamon wants that recorded as a limitation with the file named",
    "If the library doesn't cover something, I'll say so rather than fill the gap with assumption",
    "take the shorter-query route either way",
    'start the sweep with single terms — "geology," "glacial," "bedrock"',
    "open it as an experimental task rather than simply rummaging through the archive unattended",
    "need the scope spelled out plainly: book search first",
    "say so rather than fill the gap with assumption",
])
def test_procedural_statements_do_not_become_goals(objective):
    assert non_goal_reason(objective)
    assert not valid_goal_objective(objective)
    assert not review_goal_source(source_text="I'll " + objective, objective=objective).managed


@pytest.mark.parametrize("objective", [
    "write a report on Michigan geology",
    "tell Mia the fossil ages tomorrow",
    "research continental glaciation in the Great Lakes region",
    "record the measurements in the lab notebook",
    "search the archive for Michigan bedrock",
])
def test_deliverables_and_research_remain_eligible(objective):
    assert non_goal_reason(objective) is None
    assert valid_goal_objective(objective)
    assert review_goal_source(source_text="I will " + objective, objective=objective).managed


def test_recent_events_keep_adoption_order_even_when_old_event_scores_higher():
    scorer = SimpleNamespace(
        context=SimpleNamespace(world_id=uuid4()),
        score=lambda item, **kwargs: SimpleNamespace(as_dict=lambda: {"total": item["importance"]}),
    )
    events = [{"event_id": i, "importance": 10 - i, "message_text": f"event {i}"} for i in (4, 2, 5, 1, 3)]
    projected = HUDAssembler._recent_events(None, events, scorer)
    assert [event["event_id"] for event in projected] == [5, 4, 3, 2, 1]
    assert projected[-1]["relevance"]["total"] > projected[0]["relevance"]["total"]


@pytest.mark.asyncio
async def test_stored_policy_repair_cancels_generated_constraint_and_keeps_real_goal(monkeypatch):
    from contextlib import asynccontextmanager
    from aios_app.epistemic.goal_revalidation import repair_procedural_goals

    @asynccontextmanager
    async def atomic():
        yield

    owner = uuid4()
    bad, good = uuid4(), uuid4()
    rows = [{
        "goal_id": goal, "instance_id": owner, "source_node_id": uuid4(), "unit_id": uuid4(),
        "goal_text": text, "meta": {"source": "message_cognition"}, "unit_meta": {
            "objective": text, "source_text": "I will " + text,
        },
    } for goal, text in [
        (bad, "keep my own impressions out of the findings column"),
        (good, "write a report on Michigan geology"),
    ]]
    db = SimpleNamespace(atomic=atomic, fetch=AsyncMock(return_value=rows), execute=AsyncMock())
    finish = AsyncMock()
    monkeypatch.setattr("aios_app.epistemic.goals.CharacterGoalService.finish", finish)
    monkeypatch.setattr("aios_app.epistemic.goals.CharacterGoalService._invalidate", AsyncMock())
    monkeypatch.setattr("aios_app.epistemic.goals.CharacterGoalService._refresh_scene", AsyncMock())

    assert await repair_procedural_goals(db) == 2
    assert finish.await_count == 1
    assert finish.call_args.kwargs["goal_id"] == bad
    assert finish.call_args.kwargs["status"] == "cancelled"
    assert db.execute.await_count == 2


def test_parser_rejects_reporting_promise_as_a_managed_goal():
    from aios_app.epistemic.message_cognition import interpret_message

    units = interpret_message(
        "I'll keep my own impressions out of the findings column.",
        character_id="Renamon", speaker_id="Renamon",
        speaker_role="character", viewpoint_id="Renamon",
    )
    assert not [u for u in units if u.claim_kind == "GOAL" and u.meta.get("goal_admission_status") == "admit"]
