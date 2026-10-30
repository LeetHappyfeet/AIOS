"""Operator controls for the server-side workspace credential."""
from __future__ import annotations

import os
import secrets

import gradio as gr

from aios_app.launch_config import (
    get_workspace_api_key,
    launch_config_path,
    read_launch_config,
    save_workspace_api_key,
)
from aios_app.ui.registry import register_tab


def _status():
    try:
        saved = bool(read_launch_config().get("workspace_api_key"))
        overridden = "AIOS_WORKSPACE_API_KEY" in os.environ
        active = bool(get_workspace_api_key())
        return (
            f"Workspace access: **{'enabled' if active else 'disabled'}**. "
            f"Saved key: **{'configured' if saved else 'not configured'}**.\n\n"
            f"Launch configuration: `{launch_config_path()}`.\n\n"
            + ("**Environment override is present.** The API uses AIOS_WORKSPACE_API_KEY "
               "(including an empty value). Remove that setting and restart AIOS to use the saved key."
               if overridden else "The API reads the saved key automatically; no restart is needed.")
        )
    except (OSError, ValueError):
        return "Unable to read launch configuration. Check the file's format and server permissions."


def _save(key):
    try:
        save_workspace_api_key(str(key or ""))
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    except OSError:
        raise gr.Error("Could not save launch configuration. Check server file permissions.") from None
    return "", "Workspace key saved.\n\n" + _status()


def _reveal():
    try:
        return get_workspace_api_key()
    except (OSError, ValueError):
        raise gr.Error("Could not read the workspace key. Check the launch configuration.") from None


@register_tab
def render():
    with gr.Tab("Workspace Access"):
        gr.Markdown(
            "### AIOS Client connection key\n"
            "Save the server's workspace API key here, then enter the same key in AIOS Client's "
            "**AIOS connection** settings. This is separate from your model provider API key. "
            "The saved credential survives AIOS restarts.\n\n"
            "The launch configuration stores this key as plaintext with owner-only permissions "
            "on Unix. Anyone with access to this control plane can change or reveal it; "
            "keep Gradio accessible only to trusted operators."
        )
        status = gr.Markdown(_status())
        key = gr.Textbox(label="Workspace API key", type="password", value="",
                         placeholder="Enter a key, generate one, or reveal the active key")
        gr.Markdown("The field starts empty. Saving replaces the key; connected clients must use the new value.")
        with gr.Row():
            generate = gr.Button("Generate new key (unsaved)")
            reveal = gr.Button("Reveal active key for copying")
            save = gr.Button("Save workspace key", variant="primary")
            refresh = gr.Button("Refresh status")
        generate.click(fn=lambda: secrets.token_urlsafe(32), outputs=key)
        reveal.click(fn=_reveal, outputs=key)
        save.click(fn=_save, inputs=key, outputs=[key, status])
        refresh.click(fn=_status, outputs=status)
