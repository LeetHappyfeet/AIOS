from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from aios_app.agent.reminders import CharacterReminders, schedule_time, wake_decision
from aios_app.agent.temporal import TemporalTriggerStore
from aios_app.hud.temporal import reference_age


def test_model_can_choose_exact_duration_or_timestamp():
    now = datetime(2026, 11, 17, 17, tzinfo=timezone.utc)
    proposal = {"decision": "schedule", "reason": "Check the report",
                "delay_seconds": 2220, "due_at": None}
    assert schedule_time(proposal, now) == now + timedelta(minutes=37)
    assert schedule_time({**proposal, "delay_seconds": None,
                          "due_at": "2026-11-17T12:37:00-05:00"}, now) == now + timedelta(minutes=37)
    for invalid in (True, 0, -1, 1.5, 366 * 86400 + 1):
        with pytest.raises(ValueError):
            schedule_time({**proposal, "delay_seconds": invalid}, now)
    with pytest.raises(ValueError):
        schedule_time({**proposal, "due_at": now.isoformat()}, now)
    with pytest.raises(ValueError):
        schedule_time({**proposal, "delay_seconds": None, "due_at": "2026-11-17T12:37"}, now)


def test_wake_validates_decision_and_age_keeps_provenance():
    assert wake_decision({"decision": "defer", "note": " Still waiting. ", "delay_seconds": 2220}) == (
        "defer", "Still waiting.", 2220,
    )
    with pytest.raises(ValueError):
        wake_decision({"decision": "review", "note": "Ready.", "delay_seconds": 1})
    now = datetime(2026, 11, 17, 17, tzinfo=timezone.utc)
    assert reference_age(now, {"time_reference": {
        "kind": "recorded", "at": "2026-11-17T16:23:00+00:00",
    }}) == "recorded 37m ago"


@pytest.mark.asyncio
async def test_dispatch_decodes_postgres_jsonb_and_marks_generic_wake():
    trigger_id, wake_id = uuid4(), uuid4()
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[{
        "trigger_id": trigger_id, "instance_id": uuid4(), "event_type": "TIMER_DUE",
        "trigger_type": "timer", "due_at": datetime.now(timezone.utc),
        "priority": 100, "payload": '{"context":"inventory"}',
    }])
    db.execute = AsyncMock()
    store = TemporalTriggerStore(db)
    store.runtime.wake = AsyncMock(return_value=wake_id)

    assert await store.emit_due() == 1
    assert store.runtime.wake.call_args.kwargs["payload"]["context"] == "inventory"
    assert db.execute.call_args.args[1:] == (trigger_id, str(wake_id))


@pytest.mark.asyncio
async def test_due_reminder_queues_background_decision_as_same_instance(monkeypatch):
    trigger_id, instance_id = uuid4(), uuid4()
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[{
        "trigger_id": trigger_id, "instance_id": instance_id, "event_type": "TIMER_DUE",
        "trigger_type": "timer", "due_at": datetime.now(timezone.utc),
        "priority": 100, "payload": '{"reminder_goal_id":"test"}',
    }])
    db.fetchval = AsyncMock(return_value=trigger_id)
    db.atomic = asynccontextmanager(lambda: _atomic())
    enqueue = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("aios_app.pipeline.jobs.enqueue_job", enqueue)
    store = TemporalTriggerStore(db)

    assert await store.emit_due() == 1
    assert enqueue.call_args.kwargs["job_type"] == "character_reminder_due"
    assert enqueue.call_args.kwargs["payload"] == {
        "trigger_id": str(trigger_id), "instance_id": str(instance_id),
    }


async def _atomic():
    yield


@pytest.mark.asyncio
async def test_due_reminder_reopens_attention_for_the_linked_goal():
    trigger_id, instance_id, goal_id, timeline_id = (uuid4() for _ in range(4))
    now = datetime.now(timezone.utc)
    db = MagicMock()
    db.atomic = asynccontextmanager(lambda: _atomic())
    db.fetchrow = AsyncMock(return_value={
        "payload": {"reminder_goal_id": str(goal_id), "source_timeline_id": str(timeline_id)},
        "reason": "Check the report", "due_at": now, "fired_at": now,
    })
    db.fetchval = AsyncMock(return_value="active")
    reminders = CharacterReminders(db)
    reminders._check_available = AsyncMock()
    reminders._current_source_timeline = AsyncMock(return_value=timeline_id)
    reminders._goal = AsyncMock(return_value={"status": "active", "goal_text": "Review the report"})
    hud = MagicMock(source_timeline_id=timeline_id)
    reminders._infer = AsyncMock(return_value=(hud, {
        "decision": "review", "note": "The report is ready to review.", "delay_seconds": None,
    }))
    reminders.runtime.wake = AsyncMock()

    await reminders.handle_due(instance_id, trigger_id)

    assert db.fetchval.call_args.args[1:] == (
        instance_id, trigger_id, "review", "The report is ready to review.",
    )
    assert reminders.runtime.wake.call_args.kwargs["payload"]["goal_id"] == str(goal_id)
    assert reminders.runtime.wake.call_args.kwargs["dedupe_key"] == f"reminder:review:{trigger_id}"
