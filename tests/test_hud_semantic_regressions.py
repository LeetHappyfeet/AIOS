from aios_app.epistemic.message_cognition import INTERPRETER_VERSION, interpret_message
from aios_app.epistemic.semantic_interpreter import interpret_frame
from aios_app.hud.recent_events import project_recent_events, project_scene_change
from aios_app.hud.render_text import render_hud_text


def _shego_units(text: str):
    return interpret_message(
        text,
        character_id="Shego_001",
        speaker_id="Shego_001",
        speaker_role="assistant",
        viewpoint_id="Shego_001",
    )


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


def test_capitalized_event_pronoun_resolves_to_character_owner():
    units = interpret_message(
        "He moved quietly through the apartment.",
        character_id="Alex",
        speaker_id="Alex",
        speaker_role="character",
        viewpoint_id="Alex",
    )
    events = [unit for unit in units if unit.claim_kind == "EVENT"]
    assert events
    assert events[0].meta["semantic_owner"] == "Alex"
    assert not events[0].text.startswith("e:")


def test_capitalized_possessive_event_does_not_create_truncated_owner():
    units = interpret_message(
        "His deal changed when he moved here.",
        character_id="Alex",
        speaker_id="Alex",
        speaker_role="character",
        viewpoint_id="Alex",
    )
    events = [unit for unit in units if unit.claim_kind == "EVENT"]
    assert events
    assert events[0].meta["semantic_owner"] not in {"e", "is"}


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


def test_recent_semantic_event_is_rendered_as_narrative_not_fake_speaker():
    frame = {
        "identity": {"character_id": "Alex"},
        "presence": {"world_key": "test", "instance_id": "i", "state_version": 1},
        "recent_events": [{"text": "His: His deal was that he'd moved here."}],
        "actions": [],
    }
    rendered = render_hud_text(frame)
    assert "His: His deal" not in rendered
    assert "- His deal was that he'd moved here." in rendered


def test_recent_event_projection_prefers_semantic_projection_for_same_source_node():
    projected = project_recent_events(
        [
            {"node_id": "n1", "message_text": "a very long raw source turn"},
            {"source_node_id": "n1", "text": "Alex arrived at the apartment."},
        ]
    )
    assert [row["text"] for row in projected] == ["Alex arrived at the apartment."]


def test_recent_event_projection_deduplicates_identical_narrative_events():
    projected = project_recent_events(
        [
            {"text": "Alex: He opened the door."},
            {"text": "He opened the door."},
        ]
    )
    assert len(projected) == 1
    assert projected[0]["text"] == "He opened the door."



def test_scene_change_prefers_semantic_projection_over_long_source_turn():
    raw = "Shego delivers a very long speech about server power. " * 100
    projected = project_scene_change(
        [
            {"node_id": "n1", "message_text": raw},
            {
                "source_node_id": "n1",
                "text": "Shego proposes using security work to fund more hardware.",
            },
        ]
    )
    assert projected == "Shego proposes using security work to fund more hardware."
    assert raw[:100] not in projected


def test_scene_change_bounds_raw_source_when_semantics_are_not_ready():
    projected = project_scene_change(
        [{"node_id": "n1", "message_text": "RAW source transcript " * 200}],
        max_chars=120,
    )
    assert projected is not None
    assert len(projected) <= 121
    assert projected.endswith("…")


def test_renderer_does_not_repeat_immediate_goal_in_goals():
    frame = {
        "identity": {"character_id": "Shego_001"},
        "presence": {"world_key": "test", "instance_id": "i", "state_version": 1},
        "scene": {
            "working_state": {
                "immediate_goal": "Shego wants the other half of that sentence."
            }
        },
        "goals": [
            {"text": "Shego wants the other half of that sentence."},
            {"text": "Shego wants to learn game control."},
        ],
        "actions": [],
    }
    rendered = render_hud_text(frame)
    assert rendered.count("Shego wants the other half of that sentence.") == 1
    assert "Shego wants to learn game control." in rendered
