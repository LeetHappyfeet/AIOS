import json
from uuid import uuid4

import pytest

from aios_app.agent.opportunities import _json_dumps


def test_json_dumps_normalizes_nested_uuid_values():
    value = uuid4()
    encoded = _json_dumps({
        "goal_state": {
            "subject_id": value,
            "nested": [{"source_id": value}],
        }
    })

    decoded = json.loads(encoded)
    assert decoded["goal_state"]["subject_id"] == str(value)
    assert decoded["goal_state"]["nested"][0]["source_id"] == str(value)


def test_json_dumps_rejects_unknown_non_json_types():
    with pytest.raises(TypeError):
        _json_dumps({"bad": object()})
