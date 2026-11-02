"""Persistent workspace credentials: process boundary, rotation, and auth separation."""
import json
import os
import stat
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aios_app.launch_config import (
    get_workspace_api_key,
    launch_config_path,
    read_launch_config,
    save_workspace_api_key,
)
from aios_app.agent.workspaces import install_workspace_routes


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_LAUNCH_CONFIG", str(tmp_path / "launch-config.json"))
    for name in ("AIOS_WORKSPACE_API_KEY", "AIOS_WORKSPACE_REVIEW_KEY", "AIOS_WORK_REQUEST_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_persistence_across_process_and_working_directory(tmp_path):
    path = launch_config_path()
    path.write_text(json.dumps({"future_setting": True}))
    save_workspace_api_key("test-persistent-key")
    assert read_launch_config()["future_setting"] is True
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    result = subprocess.run(
        [sys.executable, "-c", "from aios_app.launch_config import get_workspace_api_key; "
         "assert get_workspace_api_key() == 'test-persistent-key'"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout


def test_environment_override_including_empty(monkeypatch):
    save_workspace_api_key("saved")
    monkeypatch.setenv("AIOS_WORKSPACE_API_KEY", "override")
    assert get_workspace_api_key() == "override"
    monkeypatch.setenv("AIOS_WORKSPACE_API_KEY", "")
    assert get_workspace_api_key() == ""
    monkeypatch.delenv("AIOS_WORKSPACE_API_KEY")
    assert get_workspace_api_key() == "saved"


@pytest.mark.parametrize("key", ["", "two words", "newline\n", "non-ascii-é"])
def test_invalid_key_preserves_saved_file(key):
    save_workspace_api_key("original")
    with pytest.raises(ValueError):
        save_workspace_api_key(key)
    assert get_workspace_api_key() == "original"


@pytest.mark.parametrize("name", ["AIOS_WORKSPACE_REVIEW_KEY", "AIOS_WORK_REQUEST_API_KEY"])
def test_rejects_operator_key_reuse(monkeypatch, name):
    monkeypatch.setenv(name, "operator")
    with pytest.raises(ValueError, match="must differ"):
        save_workspace_api_key("operator")
    assert not launch_config_path().exists()


def test_invalid_config_fails_without_echoing_contents():
    launch_config_path().write_text('{"workspace_api_key":"private-broken-secret')
    with pytest.raises(ValueError) as exc:
        read_launch_config()
    assert "private-broken-secret" not in str(exc.value)


def test_absolute_path_required(monkeypatch):
    monkeypatch.setenv("AIOS_LAUNCH_CONFIG", "relative.json")
    with pytest.raises(ValueError, match="absolute"):
        launch_config_path()


def test_api_observes_save_and_rotation_without_restart(monkeypatch):
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    with TestClient(app) as client:
        def protocol(key):
            return client.get("/workspace-client/protocol", headers={"Authorization": f"Bearer {key}"})
        assert protocol("first").status_code == 503
        save_workspace_api_key("first")
        assert protocol("first").status_code == 200
        save_workspace_api_key("second")
        assert protocol("first").status_code == 401
        assert protocol("second").status_code == 200
        # A later environment misconfiguration still fails closed at the review boundary.
        monkeypatch.setenv("AIOS_WORKSPACE_REVIEW_KEY", "second")
        response = client.get(
            f"/workspace-client/{uuid4()}/runs/{uuid4()}/review",
            headers={"Authorization": "Bearer second"},
        )
        assert response.status_code == 503


def test_gradio_tab_and_callbacks():
    gr = pytest.importorskip("gradio")
    from aios_app.ui.tabs.workspace_access import render, _save, _status, _reveal

    with gr.Blocks() as demo:
        render()
    assert len(demo.config["dependencies"]) == 4
    empty, status = _save("gradio-test-key")
    assert empty == "" and "enabled" in status
    assert "gradio-test-key" not in _status()
    assert _reveal() == "gradio-test-key"
