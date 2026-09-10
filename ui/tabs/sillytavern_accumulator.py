from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

import gradio as gr

from aios_app.accumulator.sillytavern.config import INPUT_DIR, STATE_FILE
from aios_app.accumulator.sillytavern.parser import summarize_chat_log
from aios_app.ui.registry import register_tab


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._ @()+-]+", "_", name).strip("._ ")
    return clean or "sillytavern-chat.jsonl"


def _normalize_uploaded_files(files) -> list[Path]:
    if not files:
        return []
    if not isinstance(files, (list, tuple)):
        files = [files]

    paths: list[Path] = []
    for item in files:
        value = getattr(item, "name", item)
        if value:
            paths.append(Path(str(value)))
    return paths


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed": {}, "failed": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "processed": data.get("processed", {}) or {},
            "failed": data.get("failed", {}) or {},
        }
    except Exception:
        return {"processed": {}, "failed": {}}


def _rows() -> list[list[Any]]:
    state = _load_state()
    processed = state["processed"]
    failed = state["failed"]
    rows: list[list[Any]] = []

    for path in sorted(INPUT_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            summary = summarize_chat_log(path)
            file_sha = summary["file_sha256"]
            if processed.get(str(path)) == file_sha:
                status = "processed"
            elif str(path) in failed:
                status = "failed"
            else:
                status = "queued"

            rows.append(
                [
                    path.name,
                    status,
                    summary["character_name"],
                    summary["user_name"],
                    summary["messages"],
                    summary["user_messages"],
                    summary["character_messages"],
                    summary["system_messages"],
                    summary["chat_key"],
                ]
            )
        except Exception as exc:
            rows.append([path.name, "invalid", "", "", 0, 0, 0, 0, str(exc)[:180]])
    return rows


def _stage_files(files):
    uploaded = _normalize_uploaded_files(files)
    if not uploaded:
        return "Drop one or more SillyTavern .jsonl files first.", _rows()

    accepted = []
    errors = []
    for source in uploaded:
        try:
            if source.suffix.lower() != ".jsonl":
                raise ValueError("file extension must be .jsonl")

            summary = summarize_chat_log(source)
            digest = summary["file_sha256"]
            target_name = f"{digest[:12]}-{_safe_name(source.name)}"
            target = INPUT_DIR / target_name

            if not target.exists() or _sha256(target) != digest:
                shutil.copy2(source, target)

            accepted.append(
                f"{source.name}: {summary['messages']} messages "
                f"({summary['user_name']} ↔ {summary['character_name']})"
            )
        except Exception as exc:
            errors.append(f"{source.name}: {exc}")

    messages = []
    if accepted:
        messages.append("Queued for accumulator ingestion:\n" + "\n".join(f"• {item}" for item in accepted))
    if errors:
        messages.append("Rejected:\n" + "\n".join(f"• {item}" for item in errors))

    return "\n\n".join(messages), _rows()


@register_tab
def render():
    with gr.Tab("SillyTavern Chat Import"):
        gr.Markdown(
            """
### SillyTavern JSONL Chat Accumulator

Drag exported SillyTavern .jsonl chat logs below. Files are validated and
copied into the dedicated accumulator input directory, then the accumulator
service imports each conversation into the liminal DAG as native chat messages.

The chat header supplies the active character and user identity. The selected
mes field is the message used for claim extraction; swipe/model/generation
metadata is retained as provenance rather than treated as additional turns.
"""
        )

        upload = gr.File(
            label="Drop SillyTavern .jsonl chat logs here",
            file_count="multiple",
            file_types=[".jsonl"],
            type="filepath",
        )

        with gr.Row():
            stage = gr.Button("Queue uploaded chats", variant="primary")
            refresh = gr.Button("Refresh status")

        status = gr.Textbox(label="Import status", lines=8, interactive=False)

        table = gr.Dataframe(
            headers=["File", "Status", "Character", "User", "Messages", "User msgs", "Character msgs", "System msgs", "Chat key / error"],
            datatype=["str", "str", "str", "str", "number", "number", "number", "number", "str"],
            value=_rows(),
            interactive=False,
            label="SillyTavern accumulator queue",
        )

        stage.click(fn=_stage_files, inputs=upload, outputs=[status, table])
        refresh.click(fn=_rows, inputs=None, outputs=table)
