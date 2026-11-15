"""Character activity is interpretation, not execution evidence."""
from aios_app.agent.work_activity_context import validate_brief
from aios_app.hud.work_results import result_view
from aios_app.hud.work import render_work_results


def test_brief_rejects_empty_oversized_or_fabricated_shapes():
    assert validate_brief({"summary": ""}) is None
    assert validate_brief({"summary": "I plan a game", "intentions": ["ok"] * 5}) is None
    assert validate_brief({"summary": "x" * 1401}) is None
    assert validate_brief({"summary": "I plan a game", "intentions": ["Make a prototype"],
                           "open_questions": ["How should it end?"]}) == {
        "version": "character-activity-v1",
        "perspective": "first_person",
        "summary": "I plan a game",
        "intentions": ["Make a prototype"],
        "open_questions": ["How should it end?"],
        "evidence_status": "character_narration_not_verified_execution",
    }


def test_character_report_does_not_override_worker_outcome():
    result = {
        "result_summary": {"outcome": "blocked", "summary": "No executable prototype"},
        "character_report": {
            "version": "character-activity-v1", "summary": "I began the prototype.",
            "reflection": "I still need to resolve the blocker.",
            "open_threads": ["Revisit the build"],
        },
    }
    view = result_view(result, {})
    assert view["worker_outcome"] == "blocked"
    assert view["character_report"]["summary"] == "I began the prototype."
    text = "\n".join(render_work_results(
        {"requests": []}, [{"run_id": "r1", "status": "finished",
                            "objective": "Prototype game", "result_summary": view,
                            "verification_status": "not_configured"}]))
    assert "MY COMPUTER" in text
    assert "not verified facts" in text
    assert "blocked" in text
    assert "Open thread (not an assigned goal)" in text
