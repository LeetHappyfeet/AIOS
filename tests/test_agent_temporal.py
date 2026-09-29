from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from aios_app.agent.actions import default_action_registry
from aios_app.agent.autonomy import AutonomyScheduler
from aios_app.agent.temporal import TemporalTriggerStore, resolve_goal_time_expression


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


def test_future_goal_phrase_resolves_to_timezone_aware_review_window():
    source_at = datetime.now(timezone.utc) - timedelta(hours=1)
    window = resolve_goal_time_expression(
        expression="tomorrow afternoon",
        source_text="Tomorrow afternoon. I'll go.",
        source_at=source_at,
        timezone_name="America/New_York",
    )
    assert window is not None
    assert window.timezone_name == "America/New_York"
    assert window.expression == "tomorrow afternoon"
    assert window.due_at < window.window_end_at
    assert window.due_at.astimezone(ZoneInfo("America/New_York")).hour == 12


def test_future_goal_phrase_rejects_unstated_or_vague_times():
    source_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert resolve_goal_time_expression(
        expression="tomorrow afternoon", source_text="I'll go.",
        source_at=source_at, timezone_name="America/New_York",
    ) is None
    assert resolve_goal_time_expression(
        expression="later", source_text="I'll come back later.",
        source_at=source_at, timezone_name="America/New_York",
    ) is None
    assert resolve_goal_time_expression(
        expression="tomorrow afternoon",
        source_text="Tomorrow afternoon. I'll go.",
        source_at=source_at, timezone_name=None,
    ) is None


def test_relative_duration_does_not_require_timezone():
    source_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    window = resolve_goal_time_expression(
        expression="in 2 hours", source_text="I'll return in 2 hours.",
        source_at=source_at, timezone_name=None,
    )
    assert window is not None
    assert window.due_at == source_at + timedelta(hours=2)
    assert window.window_end_at == window.due_at


def test_vague_future_revisit_uses_only_a_bounded_model_selected_interval():
    source_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert resolve_goal_time_expression(
        expression="later", source_text="I'll come back later.",
        source_at=source_at, timezone_name=None,
    ) is None
    window = resolve_goal_time_expression(
        expression="later", source_text="I'll come back later.",
        source_at=source_at, timezone_name=None, suggested_delay_seconds=3600,
    )
    assert window is not None
    assert window.due_at == source_at + timedelta(hours=1)
    assert resolve_goal_time_expression(
        expression="later", source_text="I'll come back later.",
        source_at=source_at, timezone_name=None, suggested_delay_seconds=2700,
    ) is None


class _StaleGoalWakeDB:
    def __init__(self, event):
        self.event = event
        self.executed = []

    async def fetch(self, sql, *args):
        return [self.event]

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_stale_goal_timer_wake_is_consumed_without_cognition():
    from uuid import uuid4

    db = _StaleGoalWakeDB({
        "wake_id": uuid4(), "event_type": "GOAL_REVIEW_DUE",
        "source_type": "temporal_trigger", "source_id": "trigger",
        "payload": {"goal_id": str(uuid4()), "trigger_id": str(uuid4())},
        "priority": 20,
    })
    scheduled = await AutonomyScheduler(db)._schedule_instance(INSTANCE_ID)
    assert scheduled is False
    assert len(db.executed) == 2
    assert "status='consumed'" in db.executed[0][0]
    assert "goal_changed_before_review" in db.executed[1][0]
