from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from aios_app.agent.actions import default_action_registry
from aios_app.agent.temporal import TemporalTriggerStore


def test_timer_capabilities_are_bounded_time_tools():
    registry = default_action_registry(object())
    caps = registry.capabilities_for("executive")
    assert caps["timer.set"]["class"] == "time"
    assert caps["timer.cancel"]["class"] == "time"
    assert caps["timer.list"]["class"] == "time"
    assert caps["timer.set"]["side_effect_class"] == "internal_write"
    assert caps["timer.list"]["side_effect_class"] == "read_only"
    assert "oneOf" in caps["timer.set"]["schema"]


class _TimerDB:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    async def execute_returning_row(self, sql, *args):
        self.calls.append(("returning", sql, args))
        return self.row

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.row

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return []


@pytest.mark.asyncio
async def test_timer_requires_exactly_one_time_form():
    db = _TimerDB()
    store = TemporalTriggerStore(db)
    instance = UUID("00000000-0000-0000-0000-000000000001")
    with pytest.raises(ValueError):
        await store.create_timer(instance_id=instance, reason="x")
    with pytest.raises(ValueError):
        await store.create_timer(
            instance_id=instance, reason="x", duration_seconds=5,
            due_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_absolute_timer_requires_timezone():
    db = _TimerDB()
    store = TemporalTriggerStore(db)
    with pytest.raises(ValueError):
        await store.create_timer(
            instance_id=UUID("00000000-0000-0000-0000-000000000001"),
            reason="x", due_at=datetime(2026, 9, 27, 12, 0),
        )
