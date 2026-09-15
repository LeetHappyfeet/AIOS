from __future__ import annotations

from uuid import uuid4

import pytest

import aios_app.ingest_api as ingest_api
from aios_app.models import IngestIn


class FakeDB:
    def __init__(self, event_row, fetchrows):
        self.event_row = event_row
        self.fetchrows = list(fetchrows)
        self.executes = []
        self.returning_calls = []
        self.fetchrow_calls = []

    async def execute_returning_row(self, sql, *args):
        self.returning_calls.append((sql, args))
        return self.event_row

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self.fetchrows.pop(0) if self.fetchrows else None

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "UPDATE 1"


def request(*, text="Hello", message_id=12):
    return IngestIn(
        session_id=uuid4(),
        speaker_id="Alex",
        speaker_type="character",
        recipient_id="Mia",
        character_id="Alex",
        user_name="Mia",
        text=text,
        payload={"source": "sillytavern", "message_id": message_id},
        scope_key="chat:test",
    )


def install_structural_mocks(monkeypatch, *, timeline_id=None, node_id=None):
    timeline_id = timeline_id or uuid4()
    node_id = node_id or uuid4()
    calls = {"timeline": 0, "dag": [], "dirty": []}

    async def fake_timeline(*args, **kwargs):
        calls["timeline"] += 1
        return timeline_id

    async def fake_dag(*args, **kwargs):
        calls["dag"].append(kwargs)
        return node_id, kwargs.get("parent_node_id")

    async def fake_dirty(*args, **kwargs):
        calls["dirty"].append(kwargs)

    monkeypatch.setattr(ingest_api, "get_or_create_timeline", fake_timeline)
    monkeypatch.setattr(ingest_api, "add_node_and_edge", fake_dag)
    monkeypatch.setattr(ingest_api, "mark_matching_runtime_dirty", fake_dirty)
    return timeline_id, node_id, calls


@pytest.mark.asyncio
async def test_exact_active_replay_returns_before_pipeline_side_effects(monkeypatch):
    timeline_id = uuid4()
    node_id = uuid4()
    active_head_id = uuid4()
    db = FakeDB(
        {"event_id": 41, "inserted": False, "was_superseded": False},
        [
            {"node_id": node_id, "timeline_id": timeline_id},
            {"source_head_node_id": active_head_id},
        ],
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("durable active replay must not enter structural/HUD pipeline")

    monkeypatch.setattr(ingest_api, "get_or_create_timeline", forbidden)
    monkeypatch.setattr(ingest_api, "add_node_and_edge", forbidden)
    monkeypatch.setattr(ingest_api, "mark_matching_runtime_dirty", forbidden)

    out = await ingest_api.ingest_message(db, request())

    assert out.event_id == 41
    assert out.node_id == node_id
    assert out.timeline_id == timeline_id
    assert out.disposition == "active_replay"
    assert out.source_head_node_id == active_head_id
    assert out.source_current is False
    assert db.executes == []
    assert len(db.fetchrow_calls) == 2


@pytest.mark.asyncio
async def test_exact_active_replay_reports_current_when_it_is_runtime_head(monkeypatch):
    timeline_id = uuid4()
    node_id = uuid4()
    db = FakeDB(
        {"event_id": 41, "inserted": False, "was_superseded": False},
        [
            {"node_id": node_id, "timeline_id": timeline_id},
            {"source_head_node_id": node_id},
        ],
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("durable active replay must not enter structural/HUD pipeline")

    monkeypatch.setattr(ingest_api, "get_or_create_timeline", forbidden)
    monkeypatch.setattr(ingest_api, "add_node_and_edge", forbidden)
    monkeypatch.setattr(ingest_api, "mark_matching_runtime_dirty", forbidden)

    out = await ingest_api.ingest_message(db, request())

    assert out.disposition == "active_replay"
    assert out.source_head_node_id == node_id
    assert out.source_current is True


@pytest.mark.asyncio
async def test_new_sillytavern_message_does_not_get_blanket_rewind_permission(monkeypatch):
    db = FakeDB(
        {"event_id": 42, "inserted": True, "was_superseded": False},
        [None, None],
    )
    _, _, calls = install_structural_mocks(monkeypatch)

    out = await ingest_api.ingest_message(db, request(message_id=13))

    runtime_updates = [call for call in db.executes if "character_runtime_state" in call[0]]
    assert len(runtime_updates) == 1
    assert runtime_updates[0][1][-1] is False
    assert len(calls["dag"]) == 1
    assert len(calls["dirty"]) == 1
    assert out.disposition == "new"


@pytest.mark.asyncio
async def test_changed_text_same_source_slot_is_replacement_and_can_rewind(monkeypatch):
    parent_node_id = uuid4()
    db = FakeDB(
        {"event_id": 52, "inserted": True, "was_superseded": False},
        [{"event_id": 51, "node_id": uuid4(), "parent_node_id": parent_node_id}, None],
    )
    _, _, calls = install_structural_mocks(monkeypatch)

    out = await ingest_api.ingest_message(db, request(text="Regenerated answer", message_id=20))

    assert any("superseded_at=now()" in sql for sql, _ in db.executes)
    assert calls["dag"][0]["parent_node_id"] == parent_node_id
    assert calls["dag"][0]["edge_type"] == "alternative"
    runtime_updates = [call for call in db.executes if "character_runtime_state" in call[0]]
    assert runtime_updates[0][1][-1] is True
    assert out.disposition == "new"


@pytest.mark.asyncio
async def test_old_swipe_reselection_reactivates_and_reuses_existing_event(monkeypatch):
    old_node_id = uuid4()
    parent_node_id = uuid4()
    db = FakeDB(
        {"event_id": 61, "inserted": False, "was_superseded": True},
        [{"event_id": 62, "node_id": uuid4(), "parent_node_id": parent_node_id}, None],
    )
    _, _, calls = install_structural_mocks(monkeypatch, node_id=old_node_id)

    out = await ingest_api.ingest_message(db, request(text="Old swipe", message_id=30))

    assert out.event_id == 61
    assert out.disposition == "superseded_reselection"
    assert any("SET superseded_at=NULL" in sql for sql, _ in db.executes)
    assert any("superseded_at=now()" in sql for sql, _ in db.executes)
    assert calls["dag"][0]["event_id"] == 61
    runtime_updates = [call for call in db.executes if "character_runtime_state" in call[0]]
    assert runtime_updates[0][1][-1] is True


@pytest.mark.asyncio
async def test_active_replay_without_dag_node_repairs_structural_ingest(monkeypatch):
    db = FakeDB(
        {"event_id": 71, "inserted": False, "was_superseded": False},
        [None, None, None],
    )
    _, _, calls = install_structural_mocks(monkeypatch)

    out = await ingest_api.ingest_message(db, request(message_id=40))

    assert out.event_id == 71
    assert out.disposition == "active_replay"
    assert calls["timeline"] == 1
    assert len(calls["dag"]) == 1
    assert len(calls["dirty"]) == 1
    runtime_updates = [call for call in db.executes if "character_runtime_state" in call[0]]
    assert runtime_updates[0][1][-1] is False
