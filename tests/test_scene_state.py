from uuid import uuid4

from aios_app.epistemic.scene_state import _slot_view


def test_scene_slot_view_is_instance_neutral_and_bounded():
    scene = {
        "location": {"entity_id": uuid4(), "display_name": "car"},
        "present_entities": [{"entity_id": uuid4(), "display_name": "Renamon"}],
        "relevant_objects": [],
        "immediate_goal": "leave",
        "pending_action": "reach for the key",
        "last_significant_change": "the door closed",
        "unbounded_debug_payload": {"must_not": "persist"},
    }
    projected = _slot_view(scene)
    assert set(projected) == {
        "location", "present_entities", "relevant_objects", "immediate_goal",
        "pending_action", "last_significant_change",
    }
    assert "unbounded_debug_payload" not in projected
    assert isinstance(projected["location"]["entity_id"], str)
