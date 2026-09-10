import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import aios_app.world.runtime as runtime_module
from aios_app.world.runtime import WorldRuntimeService


class FakeDB:
    async def execute(self, *_args, **_kwargs):
        return "UPDATE 1"


@pytest.fixture
def runtime_mocks(monkeypatch):
    ensure = AsyncMock()
    readiness = AsyncMock(return_value={})
    semantic = AsyncMock(return_value=False)
    enqueue = AsyncMock(return_value=1)
    set_ready = AsyncMock()
    save = AsyncMock()

    monkeypatch.setattr(runtime_module, "ensure_readiness_row", ensure)
    monkeypatch.setattr(runtime_module, "readiness_state", readiness)
    monkeypatch.setattr(runtime_module, "source_node_retrieval_ready", semantic)
    monkeypatch.setattr(runtime_module, "enqueue_live_turn_work", enqueue)
    monkeypatch.setattr(runtime_module, "set_retrieval_ready", set_ready)
    monkeypatch.setattr(runtime_module, "save_prepared_snapshot", save)

    return {
        "ensure": ensure,
        "readiness": readiness,
        "semantic": semantic,
        "enqueue": enqueue,
        "set_ready": set_ready,
        "save": save,
    }


@pytest.mark.asyncio
async def test_live_hud_builds_when_semantic_enrichment_is_stale(runtime_mocks):
    instance_id = uuid4()
    source_node_id = uuid4()
    state = {
        "instance_id": instance_id,
        "state_version": 4,
        "source_head_node_id": source_node_id,
    }

    runtime = WorldRuntimeService(FakeDB())
    runtime.get_state = AsyncMock(return_value=state)
    runtime.hud.build = AsyncMock(
        return_value={"presence": {"state_version": 4}, "hud": {}}
    )

    frame = await runtime.prepare_frame(instance_id, wait_ms=10_000)
    await asyncio.sleep(0)

    assert runtime_mocks["semantic"].await_count == 1
    assert runtime.hud.build.await_count == 1
    assert runtime_mocks["set_ready"].await_count == 0
    assert runtime_mocks["enqueue"].await_count == 1
    assert frame["hud"]["generation_ready"] is True
    assert frame["hud"]["freshness"]["topology_current"] is False
    assert frame["hud"]["freshness"]["source_current"] is True
    assert frame["hud"]["freshness"]["runtime_current"] is True


@pytest.mark.asyncio
async def test_duplicate_live_hud_requests_share_one_build(runtime_mocks):
    instance_id = uuid4()
    source_node_id = uuid4()
    state = {
        "instance_id": instance_id,
        "state_version": 9,
        "source_head_node_id": source_node_id,
    }
    build_started = asyncio.Event()
    release_build = asyncio.Event()

    async def build(*_args, **_kwargs):
        build_started.set()
        await release_build.wait()
        return {"presence": {"state_version": 9}, "hud": {}}

    runtime = WorldRuntimeService(FakeDB())
    runtime.get_state = AsyncMock(return_value=state)
    runtime.hud.build = AsyncMock(side_effect=build)

    first = asyncio.create_task(
        runtime.prepare_frame(
            instance_id,
            recent_limit=12,
            token_budget=4000,
            wait_ms=1200,
        )
    )
    await build_started.wait()
    second = asyncio.create_task(
        runtime.prepare_frame(
            instance_id,
            recent_limit=12,
            token_budget=4000,
            wait_ms=1200,
        )
    )
    await asyncio.sleep(0)

    assert runtime.hud.build.await_count == 1

    release_build.set()
    first_frame, second_frame = await asyncio.gather(first, second)
    await asyncio.sleep(0)

    assert first_frame["hud"]["generation_ready"] is True
    assert second_frame["hud"]["generation_ready"] is True
    assert runtime.hud.build.await_count == 1
    assert runtime_mocks["semantic"].await_count == 1
    assert runtime_mocks["enqueue"].await_count == 1


@pytest.mark.asyncio
async def test_different_hud_shapes_do_not_share_build(runtime_mocks):
    instance_id = uuid4()
    source_node_id = uuid4()
    state = {
        "instance_id": instance_id,
        "state_version": 2,
        "source_head_node_id": source_node_id,
    }
    release_build = asyncio.Event()

    async def build(*_args, **_kwargs):
        await release_build.wait()
        return {"presence": {"state_version": 2}, "hud": {}}

    runtime = WorldRuntimeService(FakeDB())
    runtime.get_state = AsyncMock(return_value=state)
    runtime.hud.build = AsyncMock(side_effect=build)

    compact = asyncio.create_task(
        runtime.prepare_frame(instance_id, recent_limit=8, token_budget=1600)
    )
    expanded = asyncio.create_task(
        runtime.prepare_frame(instance_id, recent_limit=20, token_budget=4000)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runtime.hud.build.await_count == 2

    release_build.set()
    await asyncio.gather(compact, expanded)
