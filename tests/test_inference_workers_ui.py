import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

gr = pytest.importorskip("gradio")


@pytest.fixture
def ui(monkeypatch):
    from aios_app.db import Database
    monkeypatch.setattr(Database, "connect", AsyncMock())
    module = importlib.import_module("aios_app.ui.tabs.inference_workers")
    monkeypatch.setattr(module, "run_async", asyncio.run)
    return module


def worker(**kwargs):
    return SimpleNamespace(**{
        "provider_id": uuid4(), "provider_key": "deepseek", "display_name": "DeepSeek",
        "base_url": "https://api.deepseek.com", "model": "deepseek-reasoner",
        "provider_type": "openai_compatible", "status": "ready", "enabled": False,
        "drain": True, "max_concurrency": 1, "context_window": 32768,
        "worker_classes": ("planning",), "strict_worker_classes": True,
        "consecutive_failures": 0, "protocol_failures": 0, "last_error": None,
        "api_key_env": "DEEPSEEK_KEY", "api_key_secret": "never-expose-this",
        "timeout_seconds": 900, "capabilities": {"thinking": True, "custom_option": "preserve"},
        **kwargs,
    })


def fake_store(ui, monkeypatch, workers):
    store = SimpleNamespace(list=AsyncMock(return_value=workers),
        get=AsyncMock(return_value=workers[0] if workers else None),
        upsert=AsyncMock(return_value=workers[0] if workers else None),
        set_control=AsyncMock(), delete=AsyncMock())
    monkeypatch.setattr(ui, "InferenceProviderStore", lambda _: store)
    return store


def test_refresh_uses_named_choices_and_preserves_selection(ui, monkeypatch):
    p = worker()
    fake_store(ui, monkeypatch, [p])
    rows, dropdown = ui._refresh(str(p.provider_id))
    assert rows[0][0] == str(p.provider_id)
    assert dropdown.value == str(p.provider_id)
    assert "DeepSeek (deepseek)" in dropdown.choices[0][0]
    assert ui._refresh("deleted-worker")[1].value is None


def test_editor_loads_capabilities_without_exposing_secret(ui, monkeypatch):
    p = worker(capabilities={"thinking": True, "api_format": "deepseek", "completion_budget": 9000})
    fake_store(ui, monkeypatch, [p])
    values = ui._load(str(p.provider_id))
    assert values[0] == p.provider_key
    assert values[5] == ""
    assert p.api_key_secret not in str(values)
    assert values[12:14] == ("Enabled", "deepseek")
    assert values[16] == 9000
    assert ui._load(None)[0] == ""


def test_save_preserves_drain_disable_unknown_capabilities_and_blank_key(ui, monkeypatch):
    p = worker()
    store = fake_store(ui, monkeypatch, [p])
    values = list(ui._load(str(p.provider_id)))
    values[16:18] = [8192, 16384]
    result = ui._save(*values)
    sent = store.upsert.await_args.kwargs
    assert sent["enabled"] is False and sent["drain"] is True
    assert sent["capabilities"]["custom_option"] == "preserve"
    assert sent["capabilities"]["thinking"] is True
    assert sent["capabilities"]["completion_budget"] == 8192
    assert sent["api_key_secret"] is None
    assert result[2].value == str(p.provider_id)


def test_actions_use_selected_uuid_and_removal_clears_selection(ui, monkeypatch):
    p = worker()
    store = fake_store(ui, monkeypatch, [p])
    ui._control(str(p.provider_id), "Drain")
    store.set_control.assert_awaited_once_with(p.provider_id, drain=True)
    store.list.return_value = []
    result = ui._control(str(p.provider_id), "Remove")
    store.delete.assert_awaited_once_with(p.provider_id)
    assert result[2].value is None and result[2].choices == []
    with pytest.raises(gr.Error, match="Select a worker"):
        ui._control(None, "Remove")


def test_gradio_wires_worker_dropdown_editor_and_refresh(ui, monkeypatch):
    fake_store(ui, monkeypatch, [worker()])
    with gr.Blocks() as app:
        ui.render()
    components = app.config["components"]
    assert any(c["type"] == "dropdown" and c["props"].get("label") == "Worker" for c in components)
    assert not any(c["type"] == "textbox" and c["props"].get("label") == "Provider ID" for c in components)
    callbacks = app.config["dependencies"]
    assert any(c["api_name"] == "_load" and len(c["outputs"]) == 19 for c in callbacks)
    assert any(c["api_name"] == "_save" and len(c["outputs"]) == 3 for c in callbacks)
