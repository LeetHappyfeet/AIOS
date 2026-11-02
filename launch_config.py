"""Persistent, installation-local launch settings shared by UI and API processes."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def launch_config_path() -> Path:
    configured = os.getenv("AIOS_LAUNCH_CONFIG")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("AIOS_LAUNCH_CONFIG must be an absolute path")
        return path
    return Path(__file__).resolve().parent / "launch-config.json"


def read_launch_config() -> dict:
    path = launch_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("AIOS launch configuration contains invalid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("AIOS launch configuration must be a JSON object")
    key = data.get("workspace_api_key", "")
    if not isinstance(key, str):
        raise ValueError("workspace_api_key must be a string")
    if key:
        _validate_key_format(key)
    return data


def _validate_key_format(key: str) -> None:
    # Bearer credentials must be printable ASCII without whitespace.
    if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("Workspace key must contain printable ASCII characters without spaces")


def validate_workspace_key(key: str) -> None:
    _validate_key_format(key)
    if key in {os.getenv("AIOS_WORKSPACE_REVIEW_KEY"), os.getenv("AIOS_WORK_REQUEST_API_KEY")}:
        raise ValueError("Workspace key must differ from the review and work request keys")


def get_workspace_api_key() -> str:
    # Do not export the saved value into os.environ: inherited values would mask
    # later Gradio edits in the separately running API process.
    override = os.getenv("AIOS_WORKSPACE_API_KEY")
    if override is not None:
        return override
    return read_launch_config().get("workspace_api_key", "")


def save_workspace_api_key(key: str) -> None:
    validate_workspace_key(key)
    data = read_launch_config()
    data["workspace_api_key"] = key
    path = launch_config_path()
    # Write in the destination directory, with private permissions from creation,
    # then replace atomically so API requests cannot see a partial JSON document.
    fd, temporary = tempfile.mkstemp(prefix=".launch-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
