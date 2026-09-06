import pytest

from aios_app.epistemic.knowledge import _json_object


def test_json_object_accepts_asyncpg_json_string():
    assert _json_object('{"source":"context-resolver-v1","source_key":"chat"}') == {
        "source": "context-resolver-v1",
        "source_key": "chat",
    }


def test_json_object_accepts_mapping_and_none():
    original = {"source_key": "document"}
    decoded = _json_object(original)

    assert decoded == original
    assert decoded is not original
    assert _json_object(None) == {}


def test_json_object_rejects_non_object_json():
    with pytest.raises(ValueError, match="expected JSON object metadata"):
        _json_object('["not","an","object"]')
