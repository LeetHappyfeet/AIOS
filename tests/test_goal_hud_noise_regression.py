"""Regression coverage for bounded goal admission and lean HUD presentation."""
from types import SimpleNamespace
from uuid import uuid4

from aios_app.agent.opportunities import _automatic_goal_research_eligible
from aios_app.epistemic.goal_source_admission import review_goal_source
from aios_app.hud.render_text import render_hud_text


def test_scene_horizon_never_promotes_to_durable_managed_goal():
    decision = review_goal_source(
        source_text="I will voice the order at the counter.",
        objective="voice the order at the counter", horizon="scene",
    )
    assert not decision.managed
    assert decision.reason == "transient_intention_horizon"


def test_persistent_concrete_goal_remains_eligible():
    decision = review_goal_source(
        source_text="I plan to install a USB-C port on the case.",
        objective="install a USB-C port on the case", horizon="persistent",
    )
    assert decision.managed


def test_automatic_goal_research_excludes_scene_actions_and_includes_learning():
    assert not _automatic_goal_research_eligible(SimpleNamespace(
        text="Renasuit intends to voice the order", meta={"horizon": "session"}))
    assert not _automatic_goal_research_eligible(SimpleNamespace(
        text="Learn electronics", meta={"horizon": "scene"}))
    assert _automatic_goal_research_eligible(SimpleNamespace(
        text="Learn electronics", meta={"horizon": "session"}))
    assert _automatic_goal_research_eligible(SimpleNamespace(
        text="Renasuit intends to research USB-C controllers", meta={}))


def test_other_goals_hide_internal_retrieval_status_and_opaque_references():
    immediate, other = str(uuid4()), str(uuid4())
    frame = {
        "identity": {"display_name": "Test"},
        "presence": {"instance_id": str(uuid4())},
        "scene": {"working_state": {"immediate_goal": {
            "goal_id": immediate, "text": "Research USB-C controllers",
            "knowledge_requirements": [{"question": "Which controller?",
                                        "coverage_status": "unavailable"}],
        }}},
        "goals": [
            {"goal_id": immediate, "text": "Research USB-C controllers"},
            {"goal_id": other, "text": "Order lunch",
             "knowledge_requirements": [{"coverage_status": "unavailable"}]},
        ],
    }
    hud = render_hud_text(frame)
    assert "Order lunch" in hud
    assert "[knowledge: unavailable]" not in hud
    assert "Goal knowledge (" not in hud
    assert "[goal_ref:" not in hud

    frame["work"] = {"capabilities": [{
        "capability": "workspace", "purpose": "experimental_task",
        "description": "Work on a task", "online": True,
    }]}
    hud = render_hud_text(frame)
    assert hud.count("[goal_ref:") == 1
    assert "Order lunch" in hud
