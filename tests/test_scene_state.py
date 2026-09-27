from uuid import uuid4

from aios_app.epistemic.scene_state import _slot_view


def test_scene_slot_view_is_instance_neutral_and_bounded():
    scene = {
        "location": {"entity_id": uuid4(), "display_name": "car"},
        "present_entities": [{"entity_id": uuid4(), "display_name": "Renamon"}],
        "relevant_objects": [],
        "immediate_goal": {"goal_id": str(uuid4()), "text": "leave"},
        "pending_work": {"kind": "action", "text": "reach for the key"},
        "last_significant_change": "the door closed",
        "unbounded_debug_payload": {"must_not": "persist"},
    }
    projected = _slot_view(scene)
    assert set(projected) == {
        "location", "present_entities", "immediate_goal",
        "pending_work", "last_significant_change",
    }
    assert "unbounded_debug_payload" not in projected
    assert isinstance(projected["location"]["entity_id"], str)
