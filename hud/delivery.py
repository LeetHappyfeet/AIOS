"""Fresh delivery metadata, independent of cached cognitive HUD snapshots."""
from __future__ import annotations

from hashlib import sha256
import json


def delivery_response(instance_id, frame, state, render_text):
    """Finalize a private frame copy after task overlays and a boundary recheck.

    Workspace event sequences are per-run, so their maximum alone is not a
    freshness token. Hash all delivered overlay content and canonical text too.
    """
    hud = frame.setdefault("hud", {})
    freshness = hud.setdefault("freshness", {})
    source_same = (
        str(freshness.get("source_head_node_id")) == str(state.get("source_head_node_id"))
        and str(freshness.get("source_timeline_id")) == str(state.get("source_timeline_id"))
    )
    runtime_same = freshness.get("runtime_state_version") == state.get("state_version")
    cognition_same = hud.get("cognitive_revision") == state.get("cognitive_revision")
    profile_same = hud.get("profile_revision") == state.get("profile_revision")
    unchanged = source_same and runtime_same and cognition_same and profile_same
    hud["generation_ready"] = bool(hud.get("generation_ready") and unchanged)
    if not unchanged:
        freshness["runtime_current"] = False
        freshness["source_current"] = source_same
        freshness["delivery_current"] = False
        freshness["active_source_head_node_id"] = state.get("source_head_node_id")
        freshness["delivery_reason"] = "runtime_changed_during_delivery"
        if not source_same:
            frame.pop("workspace_memory", None)
    else:
        freshness["delivery_current"] = True
    freshness.update({
        "cognitive_revision": hud.get("cognitive_revision"),
        "profile_revision": hud.get("profile_revision"),
        "task_event_watermark": hud.get("task_event_watermark", 0),
    })
    text = render_text(frame)
    revision = sha256(json.dumps({
        "instance_id": str(instance_id), "freshness": {k: v for k, v in freshness.items() if k != "hud_revision"},
        "generation_ready": hud["generation_ready"],
        "work": frame.get("work"), "remote_tasks": frame.get("remote_tasks"),
        "workspace_memory": frame.get("workspace_memory"),
        "text": text,
    }, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()
    freshness["hud_revision"] = revision
    return {
        "instance_id": instance_id, "generation_ready": hud["generation_ready"],
        "freshness": freshness, "frame": frame, "text": text,
        "hud_revision": revision,
        "task_event_watermark": hud.get("task_event_watermark", 0),
    }
