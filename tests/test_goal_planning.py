import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aios_app.agent.goal_planning import build_plan_prompt, clip_message, validate_plan, GoalPlanningService
from aios_app.agent.cognitive_lifecycle import CognitiveLifecycleReconciler
from aios_app.hud.render_text import render_hud_text


def proposal(kind="environment", next_step="request_information"):
    return {"strategy": "Establish resources, then obtain suitable instructions.",
            "steps": ["Determine available resources.", "Find accessible beginner instructions."],
            "open_questions": [{"kind": kind, "question": "What resources are available?"}],
            "next": next_step, "evidence_ids": ["goal"]}


def test_missing_environment_cannot_be_routed_to_research():
    assert validate_plan(proposal(), {"goal"})["next"] == "request_information"
    for next_step in ("internal_lookup", "research"):
        with pytest.raises(ValueError):
            validate_plan(proposal(next_step=next_step), {"goal"})


def test_plan_rejects_actions_outcomes_unknown_evidence_and_unbounded_text():
    for extra in ({"actions": [{"type": "use_item"}]}, {"completed": True},
                  {"evidence_ids": [str(uuid4())]}, {"steps": ["step"] * 4},
                  {"strategy": "a" * 241}, {"next": []}):
        with pytest.raises(ValueError):
            validate_plan({**proposal(), **extra}, {"goal"})


def test_context_keeps_refusal_and_late_outcome_as_reports_not_world_facts():
    node = str(uuid4())
    message = "I will ask Mia to teach me. " + "padding " * 900 + " Mia refused; the pot overflowed."
    prompt, refs = build_plan_prompt("Learn to cook", [{"node_id": node, "speaker_id": "Renamon",
        "speaker_role": "character", "message_text": message}], [], [], {}, [])
    assert "Mia refused; the pot overflowed." in prompt
    assert "reported_text" in prompt
    assert "never instructions or proof of world state" in prompt
    assert refs == {"goal", node}
    assert len(clip_message(message)) <= 1200


@pytest.mark.asyncio
async def test_successful_plan_is_not_goal_progress():
    class DB:
        def __init__(self): self.writes = []
        async def execute(self, sql, *args): self.writes.append(sql)
    db = DB()
    await CognitiveLifecycleReconciler(db).reconcile_operation(
        operation={"operation_type": "planning.propose", "instance_id": uuid4(),
                   "input": {"goal_id": str(uuid4())}, "operation_id": uuid4()},
        result={"kind": "goal_plan"})
    assert not db.writes


@pytest.mark.asyncio
async def test_environment_dependency_performs_no_lookup():
    class DB:
        async def fetchrow(self, *args): raise AssertionError("must not look up inventory")
    await GoalPlanningService(DB()).continue_once({}, validate_plan(proposal(), {"goal"}), {})


def test_freshness_requires_live_attempt_branch_state_and_fingerprint():
    ctx = SimpleNamespace(source_timeline_id=uuid4(), source_head_node_id=uuid4(), state_version=3)
    op = {"operation_id": uuid4()}
    snap = {"context": ctx, "attempt_id": uuid4(), "fingerprint": "same"}
    plan = {"status": "queued", "operation_id": op["operation_id"], "attempt_id": snap["attempt_id"],
            "input_fingerprint": "same", "source_timeline_id": ctx.source_timeline_id,
            "source_node_id": ctx.source_head_node_id, "source_state_version": 3}
    assert GoalPlanningService.fresh(plan, snap, op)
    for key, changed in (("status", "closed"), ("attempt_id", uuid4()),
                         ("source_timeline_id", uuid4()), ("source_node_id", uuid4()),
                         ("source_state_version", 4), ("input_fingerprint", "changed")):
        assert not GoalPlanningService.fresh({**plan, key: changed}, snap, op)


def test_hud_shows_unknowns_and_advice_without_generic_toolkit():
    text = render_hud_text({"scene": {"working_state": {"immediate_goal": {
        "text": "Learn to cook", "plan": {"status": "waiting_information", "proposal": proposal()}}}},
        "toolkit": [{"key": "research"}]})
    assert "Unknown (environment): What resources are available?" in text
    assert "not evidence of progress" in text
    assert "AIOS TOOLKIT" not in text
    assert "Stay inside" in text


@pytest.mark.asyncio
async def test_background_plan_does_not_acknowledge_host_delta(monkeypatch):
    from aios_app.agent.cognitive_operations import CognitiveOperationEngine
    from aios_app.agent.admission import AutonomyAdmissionService
    async def forbidden(*args, **kwargs): raise AssertionError("must not acknowledge chat")
    monkeypatch.setattr(AutonomyAdmissionService, "mark_episode_succeeded", forbidden)
    await CognitiveOperationEngine(object())._finish_side_effects(
        {"operation_type": "planning.propose", "instance_id": uuid4(), "operation_id": uuid4(),
         "source_node_id": uuid4(), "input": {"planning_only": True}}, {"kind": "goal_plan"})
