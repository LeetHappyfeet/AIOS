from aios_app.epistemic.message_cognition import INTERPRETER_VERSION, interpret_message
from aios_app.epistemic.semantic_interpreter import interpret_frame
from aios_app.hud.render_text import render_hud_text


def _shego_units(text: str):
    return interpret_message(
        text,
        character_id="Shego_001",
        speaker_id="Shego_001",
        speaker_role="assistant",
        viewpoint_id="Shego_001",
    )


def test_message_cognition_version_recomputes_v2_units():
    assert INTERPRETER_VERSION == "message-cognition-v3"


def test_completed_try_is_not_a_durable_goal():
    units = _shego_units("She tried to move her hand and felt it respond.")
    assert all(unit.claim_kind != "GOAL" for unit in units)


def test_should_not_is_not_promoted_to_hard_world_rule():
    units = _shego_units("Shego shouldn't just leave the room to see what happens.")
    assert all(unit.claim_kind != "RULE" for unit in units)


def test_negated_state_keeps_negation_in_canonical_text():
    units = _shego_units("It wasn't real.")
    states = [unit for unit in units if unit.claim_kind == "STATE"]
    assert states
    assert states[0].polarity == -1
    assert "not real" in states[0].text.lower()


def test_slow_interpreter_treats_try_as_event():
    interpreted = interpret_frame(
        predicate="try",
        predicate_surface="tried",
        subject="Shego",
        object_value="move her hand",
        frame_role="root",
        resolution_status="resolved",
        object_frame_id=None,
        meta={},
    )
    assert interpreted.semantic_type == "ACTION"
    assert interpreted.claim_kind == "EVENT"


def test_recent_event_renderer_prefers_budget_clipped_text():
    frame = {
        "identity": {"character_id": "Shego_001"},
        "presence": {"world_key": "test", "instance_id": "i", "state_version": 1},
        "recent_events": [
            {
                "message_text": "RAW " * 2000,
                "text": "bounded recent event",
                "event_stream": "source",
                "speaker_role": "user",
                "speaker_id": "Ren-119",
            }
        ],
        "actions": [],
    }
    rendered = render_hud_text(frame)
    assert "bounded recent event" in rendered
    assert "RAW RAW RAW" not in rendered
