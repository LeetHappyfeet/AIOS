"""Profile/focus and peer-knowledge revisions reach the actual HUD preparation."""

from unittest.mock import AsyncMock
from uuid import uuid4

import aios_app.world.runtime as module
import pytest
from aios_app import world
from aios_app.hud.frame import HUD_VERSION
from aios_app.world.runtime import WorldRuntimeService


class DB:
    async def execute(self, *_args, **_kwargs):
        return "UPDATE 1"


def service(monkeypatch, revision="r1", cached=None):
    instance, node = uuid4(), uuid4()
    state = {
        "instance_id": instance,
        "source_head_node_id": node,
        "state_version": 1,
        "source_timeline_id": uuid4(),
        "cognitive_revision": revision,
    }
    runtime = WorldRuntimeService(DB())
    runtime.get_state = AsyncMock(return_value=state)
    runtime.hud.build = AsyncMock(
        return_value={"presence": {"state_version": 1}, "hud": {"version": HUD_VERSION}}
    )
    monkeypatch.setattr(module, "ensure_readiness_row", AsyncMock())
    monkeypatch.setattr(module, "readiness_state", AsyncMock(return_value=cached or {}))
    monkeypatch.setattr(
        module, "source_node_retrieval_ready", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(module, "set_retrieval_ready", AsyncMock())
    monkeypatch.setattr(module, "save_prepared_snapshot", AsyncMock())
    return runtime, state


@pytest.mark.asyncio
async def test_execution_profile_and_assignment_focus_are_forwarded(monkeypatch):
    runtime, state = service(monkeypatch)
    frame = await world._original_prepare_frame(
        runtime,
        state["instance_id"],
        profile_name="workspace_execution",
        focus_text="Fix permission error",
        token_budget=2500,
    )
    assert runtime.hud.build.call_args.kwargs["profile_name"] == "workspace_execution"
    assert runtime.hud.build.call_args.kwargs["focus_text"] == "Fix permission error"
    assert frame["hud"]["cognitive_revision"] == "r1"
    assert frame["hud"]["request_shape"] == [
        None,
        2500,
        "workspace_execution",
        "Fix permission error",
    ]
    assert frame["hud"]["generation_ready"] is True


@pytest.mark.asyncio
async def test_peer_update_rebuilds_same_source_cached_hud(monkeypatch):
    runtime, state = service(monkeypatch, revision="r2")
    cached = {
        "status": "ready",
        "prepared_source_node_id": state["source_head_node_id"],
        "prepared_state_version": 1,
        "hud_json": {
            "hud": {
                "version": HUD_VERSION,
                "request_shape": [None, None, None, None],
                "cognitive_revision": "r1",
            }
        },
    }
    module.readiness_state.return_value = cached
    await world._original_prepare_frame(runtime, state["instance_id"])
    assert runtime.hud.build.await_count == 1


@pytest.mark.asyncio
async def test_unchanged_revision_reuses_matching_shape(monkeypatch):
    runtime, state = service(monkeypatch)
    cached = {
        "status": "ready",
        "prepared_source_node_id": state["source_head_node_id"],
        "prepared_state_version": 1,
        "hud_json": {
            "hud": {
                "version": HUD_VERSION,
                "request_shape": [None, None, None, None],
                "cognitive_revision": "r1",
            }
        },
    }
    module.readiness_state.return_value = cached
    frame = await world._original_prepare_frame(runtime, state["instance_id"])
    assert frame["hud"]["cache"] == "prepared"
    assert runtime.hud.build.await_count == 0


@pytest.mark.asyncio
async def test_revision_changes_during_build_prevent_generation_ready(monkeypatch):
    runtime, state = service(monkeypatch)
    runtime.get_state = AsyncMock(
        side_effect=[state, {**state, "cognitive_revision": "r2"}]
    )
    frame = await world._original_prepare_frame(runtime, state["instance_id"])
    assert frame["hud"]["generation_ready"] is False
    assert module.save_prepared_snapshot.await_count == 0


@pytest.mark.asyncio
async def test_live_readiness_wrapper_preserves_profile_inputs(monkeypatch):
    runtime, state = service(monkeypatch)
    monkeypatch.setattr(world, "_coalesce_live_generation", AsyncMock())
    monkeypatch.setattr(
        world, "source_node_retrieval_ready", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        world, "source_node_topology_ready", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(world, "readiness_state", AsyncMock(return_value={}))
    monkeypatch.setattr(world, "set_retrieval_ready", AsyncMock())
    await runtime.prepare_frame(
        state["instance_id"],
        profile_name="workspace_execution",
        focus_text="Assignment",
    )
    assert runtime.hud.build.call_args.kwargs["focus_text"] == "Assignment"
