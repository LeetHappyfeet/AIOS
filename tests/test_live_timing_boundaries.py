from __future__ import annotations

from uuid import uuid4

import pytest

import aios_app.world as world
from aios_app.hud import readiness as readiness


@pytest.mark.asyncio
async def test_ingest_dirty_hook_does_not_enqueue_live_work(monkeypatch):
    instance_id = uuid4()
    timeline_id = uuid4()
    node_id = uuid4()
    calls: list[tuple[str, object]] = []

    async def fake_advance(*args, **kwargs):
        calls.append(("advance", kwargs["source_head_node_id"]))
        return [instance_id]

    async def fake_mark(*args, **kwargs):
        calls.append(("dirty", kwargs["source_head_node_id"]))

    async def forbidden_enqueue(*args, **kwargs):
        raise AssertionError("historical ingest must not promote LIVE work")

    monkeypatch.setattr(readiness, "advance_matching_runtime_source_cursor", fake_advance)
    monkeypatch.setattr(readiness, "mark_source_dirty", fake_mark)
    monkeypatch.setattr(readiness, "enqueue_live_turn_work", forbidden_enqueue)

    await readiness.mark_matching_runtime_dirty(
        object(),
        character_id="Alex",
        session_id=uuid4(),
        user_name="Mia",
        scope_key="chat:test",
        source_timeline_id=timeline_id,
        source_head_node_id=node_id,
        source_head_event_id=7,
    )

    assert calls == [("advance", node_id), ("dirty", node_id)]


class _FakeDB:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 6"


@pytest.mark.asyncio
async def test_coalesce_demotes_queued_live_jobs_for_instance():
    db = _FakeDB()
    instance_id = uuid4()
    node_id = uuid4()

    changed = await world._coalesce_live_generation(
        db,
        instance_id=instance_id,
        node_id=node_id,
    )

    assert changed == 6
    sql, args = db.calls[0]
    assert "status='queued'" in sql
    assert "payload->>'live_instance_id'=$1" in sql
    assert "payload=payload - 'live_instance_id'" in sql
    assert "GREATEST(priority,35)" in sql
    assert args == (str(instance_id),)


def test_runtime_package_installs_activation_detach_hooks():
    assert getattr(world._runtime, "_activation_enqueue_detached_v1", False) is True
    assert getattr(world.WorldRuntimeService, "_live_readiness_v3", False) is True
    assert getattr(readiness, "_head_only_dirty_v1", False) is True
