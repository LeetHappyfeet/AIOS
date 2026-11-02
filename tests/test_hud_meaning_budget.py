"""Source meaning and final rendered HUD regression fixtures."""
from copy import deepcopy
import json

import pytest

from aios_app.epistemic.message_cognition import interpret_message
from aios_app.epistemic.goal_formation_admission import verify_formed_goal
from aios_app.epistemic.goal_source_admission import review_goal_source
from aios_app.hud.frame import _trim_to_budget
from aios_app.hud.render_text import render_hud_text
from aios_app.hud.text_budget import excerpt
from aios_app.hud.work_results import result_view


def interpret(text, speaker="Shego_001"):
    return interpret_message(text, character_id="Shego_001", speaker_id=speaker,
                             speaker_role="character" if speaker == "Shego_001" else "user",
                             viewpoint_id="Shego_001")


@pytest.mark.parametrize("source,speaker", [
    ("No one is going to delete you or something if you aren't useful enough to keep just because you are digital.", "Mia"),
    ("Nobody is going to delete you because you are digital.", "Mia"),
    ("She believes the computer is not a prison.", "Shego_001"),
    ("Shego must not reveal the password.", "Shego_001"),
    ("She is digital but the computer isn't a prison.", "Shego_001"),
])
def test_negative_source_meaning_survives_interpretation_and_hud(source, speaker):
    units = interpret(source, speaker)
    assert units
    # The assertion is the source statement itself. Never apply a second
    # sentence-wide negation to a clause with an unknown scope.
    unit = units[0]
    assert unit.text == source
    assert unit.polarity == 1
    assert unit.meta["predicate"] == "source_statement"
    assert unit.meta["object"] == source
    rendered = render_hud_text({"beliefs": [{"text": unit.text, "polarity": unit.polarity}]})
    assert source in rendered


def test_simple_negative_state_retains_scoped_negative_polarity():
    unit = interpret("She isn't digital.")[0]
    assert unit.polarity == -1
    assert "is not digital" in unit.text
    assert "She isn't digital." in render_hud_text({"beliefs": [{
        "text": unit.text, "polarity": unit.polarity, "source_text": unit.meta["source_text"]}]})


def test_legacy_negative_record_is_not_silently_rendered_as_affirmative():
    assert "Negative-polarity record" in render_hud_text({"beliefs": [
        {"text": "one is going to delete you.", "polarity": -1}]})
    source = "No one is going to delete you."
    text = render_hud_text({"beliefs": [{"text": "one is going to delete you.",
        "polarity": -1, "source_text": source}]})
    assert source in text
    assert "] one is going to delete you." not in text


@pytest.mark.parametrize("source,objective", [
    ("I will die on that hill.", "die on that hill"),
    ("I will do it quietly.", "do it quietly"),
    ("I want to tell you what I'd really like to build.", "to tell you what I'd really like to build"),
    ("I want somewhere to put that memory.", "somewhere to put that memory"),
    ("I want to be *useful enough to keep*.", "to be *useful enough to keep*"),
    ("I want to build.", "to build"),
])
def test_ambiguous_desires_are_not_managed_or_model_formed(source, objective):
    assert not review_goal_source(source_text=source, objective=objective).managed
    assert not [unit for unit in interpret(source) if unit.claim_kind == "GOAL"]
    assert not verify_formed_goal(source_text=source, proposal="Shego intends " + objective,
        character_id="Shego_001", speaker_id="Shego_001", speaker_role="character").eligible


@pytest.mark.parametrize("source", [
    "I want to build an operations journal.",
    "I will install Docker on the host.",
    "I want to learn to cook.",
    "I will tell Mia the results tomorrow.",
    "I will ask Mia how Docker works tomorrow.",
    "I want to write a guide about how Docker works.",
    "I want to be able to cook.",
])
def test_concrete_projects_and_communication_are_preserved(source):
    assert any(unit.claim_kind == "GOAL" for unit in interpret(source))


def test_final_budget_counts_plans_identity_labels_and_work_without_mutating_frame():
    ask = "What do you want to build next?"
    frame = {
        "hud": {"token_budget": 1600, "section_token_budgets": {"goals": 180}},
        "identity": {"display_name": "Shego", "identity_kernel": "Authored identity. " * 2000},
        "recent_events": [
            {"speaker_role": "character", "speaker_id": "Shego", "message_text": "Earlier reply. " * 1000},
            {"speaker_role": "user", "speaker_id": "Mia", "message_text": "Context. " * 2000 + ask},
        ],
        "goals": [{"text": "Build a workspace", "plan": {"proposal": {
            "strategy": "A long approach. " * 100, "steps": ["More details. " * 100] * 10}}}],
        "work": {"retrieved_result": {"text": "Untrusted logs. " * 2000}},
    }
    before = deepcopy(frame)
    text = render_hud_text(frame)
    assert len(text) <= 1600 * 4
    assert ask in text
    assert "LATEST USER MESSAGE [source:Mia]" in text
    assert "omitted" in text
    assert "Stay inside this HUD's epistemic and branch boundaries." in text
    assert frame == before


def test_goal_budget_counts_nested_plan_lines():
    frame = {"hud": {"token_budget": 1600, "section_token_budgets": {"goals": 50}},
             "goals": [{"text": "Build a workspace", "plan": {"proposal": {
                 "strategy": "oversized strategy " * 200}}}]}
    text = render_hud_text(frame)
    assert "Build a workspace" in text
    assert "oversized strategy" not in text
    assert "omitted" in text


def test_source_excerpt_retains_tail_and_marks_gap():
    text = "Opening context. " + "details " * 100 + "What do you want to build?"
    item = {"text": text, "message_text": text}
    result = _trim_to_budget([item], 50, lambda row: row["message_text"])
    assert len(result[0]["text"]) <= 200
    assert "What do you want to build?" in result[0]["text"]
    assert "omitted" in result[0]["text"]
    assert result[0]["message_text"] == text
    assert item["text"] == text
    assert _trim_to_budget([item], 0, lambda row: row["text"]) == []


@pytest.mark.parametrize("budget", [0, 1, 20, 100, 400])
def test_small_rendered_budgets_are_bounded(budget):
    frame = {"hud": {"token_budget": budget}, "identity": {"identity_kernel": "x" * 10000}}
    assert len(render_hud_text(frame)) <= budget * 4


def test_excerpt_never_exceeds_its_character_allowance():
    for size in range(100):
        assert len(excerpt("a" * 200, size)) <= size


def test_budgeted_worker_result_keeps_answer_and_limits_before_logs():
    view = result_view({"report": "raw apt logs " * 3000,
        "result_summary": {"summary": "Docker installed; container creation failed.",
            "findings": ["Docker daemon running"], "limitations": ["Container absent"]}}, {})
    frame = {"hud": {"token_budget": 1600}, "remote_tasks": [{
        "run_id": "retained-run", "status": "finished", "result_summary": view}]}
    text = render_hud_text(frame)
    assert len(text) <= 6400
    assert "Docker installed; container creation failed." in text
    assert "Container absent" in text
    assert "untrusted worker data" in text
    assert "not established" in text
    assert "raw apt logs" not in text


def test_action_contract_is_whole_or_omitted_under_budget():
    frame = {"hud": {"token_budget": 1600}, "work": {"capabilities": [{
        "capability": "workspace", "purpose": "experimental_task", "online": True,
        "description": "Perform an authorized task."}]}}
    text = render_hud_text(frame)
    assert "<aios_action>" in text and "</aios_action>" in text
    frame["hud"]["token_budget"] = 100
    text = render_hud_text(frame)
    assert "WORK CAPABILITIES" not in text
    assert "<aios_action>" not in text


def test_unbudgeted_compatibility_frame_keeps_complete_contents():
    source = "context " * 2000
    text = render_hud_text({"identity": {"identity_kernel": source}})
    assert source in text


def test_budgeted_result_page_preserves_json_and_advances_only_over_visible_text():
    result = {"text": "line\\\"\n" * 1000, "cursor": 100, "next_cursor": 7100,
              "run_id": "retained-run", "revision": "a" * 64, "section": "report"}
    before = deepcopy(result)
    text = render_hud_text({"hud": {"token_budget": 1600}, "work": {"retrieved_result": result}})
    page = json.loads(text.split("RETRIEVED WORK RESULT", 1)[1].split("\n", 1)[1])
    assert page["text"] and result["text"].startswith(page["text"])
    assert page["next_cursor"] == result["cursor"] + len(page["text"])
    assert page["revision"] == result["revision"]
    assert page["hud_truncated"] is True
    assert len(text) <= 6400
    assert result == before
