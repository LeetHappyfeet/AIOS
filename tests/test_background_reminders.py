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
    db.execute = AsyncMock()
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


@pytest.mark.asyncio
async def test_invalid_decisions_retry_before_terminal_failure():
    from aios_app.pipeline.jobs import JobDeferred

    db = MagicMock()
    db.atomic = asynccontextmanager(lambda: _atomic())
    db.fetchval = AsyncMock(side_effect=[1, 2, 3, uuid4()])
    instance_id, trigger_id = uuid4(), uuid4()
    reminders = CharacterReminders(db)
    for delay in (30, 120):
        with pytest.raises(JobDeferred) as error:
            await reminders._retry_invalid(instance_id, trigger_id)
        assert error.value.delay_seconds == delay
    await reminders._retry_invalid(instance_id, trigger_id)
    assert db.fetchval.call_args.args[1:4] == (instance_id, trigger_id, "review failed")


@pytest.mark.asyncio
async def test_inactive_timeline_suspends_before_goal_lookup_or_inference():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={
        "payload": {"source_timeline_id": str(uuid4()), "reminder_goal_id": str(uuid4())},
    })
    db.execute = AsyncMock()
    reminders = CharacterReminders(db)
    reminders._check_available = AsyncMock()
    reminders._current_source_timeline = AsyncMock(return_value=uuid4())
    reminders._goal = AsyncMock()
    reminders._infer = AsyncMock()
    instance_id, trigger_id = uuid4(), uuid4()

    await reminders.handle_due(instance_id, trigger_id)

    assert "status='timeline_inactive'" in db.execute.call_args.args[0]
    assert db.execute.call_args.args[1:] == (instance_id, trigger_id)
    reminders._goal.assert_not_awaited()
    reminders._infer.assert_not_awaited()


@pytest.mark.asyncio
async def test_continuation_requests_planning_for_its_own_goal():
    from aios_app.agent.reminder_continuation import ReminderContinuation
    from aios_app.pipeline.jobs import JobDeferred

    instance_id, goal_id, attempt_id, trigger_id, timeline_id = (uuid4() for _ in range(5))
    continuation_row = {
        "instance_id": instance_id, "goal_id": goal_id, "attempt_id": attempt_id,
        "trigger_id": trigger_id, "source_timeline_id": timeline_id,
        "active_timeline": timeline_id, "intent": None, "status": "pending", "work_request_id": None,
    }
    db = MagicMock()
    db.fetchrow = AsyncMock(side_effect=[continuation_row, None, None])
    db.fetchval = AsyncMock(side_effect=[True, True])
    db.execute = AsyncMock()
    service = ReminderContinuation(db)
    service.planner.snapshot = AsyncMock(return_value={
        "context": MagicMock(source_timeline_id=timeline_id), "attempt_id": attempt_id,
        "fingerprint": "revision-1",
    })
    service.planner.schedule = AsyncMock()

    await service.run(trigger_id)

    service.planner.schedule.assert_awaited_once_with(instance_id, goal_id)
    assert db.execute.call_args.args[2] == "waiting_plan"


@pytest.mark.asyncio
async def test_continuation_reuses_persisted_intent(monkeypatch):
    from aios_app.agent.reminder_continuation import ReminderContinuation, request_id

    instance_id, goal_id, trigger_id, source_node_id, canonical = (uuid4() for _ in range(5))
    db = MagicMock()
    db.execute = AsyncMock()
    submit = AsyncMock(return_value={"status": "waiting_client", "request_id": str(canonical)})
    monkeypatch.setattr("aios_app.agent.work_admission.WorkAdmission.request", submit)
    row = {
        "trigger_id": trigger_id, "instance_id": instance_id,
        "intent": {"request_id": str(request_id(trigger_id)), "source_node_id": str(source_node_id),
                   "goal_ref": str(goal_id), "capability": "workspace", "purpose": "task",
                   "objective": "Review the inventory report"},
    }

    await ReminderContinuation(db)._submit(row)

    args = submit.call_args
    assert args.args[1].request_id == request_id(trigger_id)
    assert args.kwargs["reminder_trigger_id"] == trigger_id
    assert db.execute.call_args.args[1:4] == (trigger_id, canonical, "waiting_client")
    assert "status='waiting_work'" in db.execute.call_args.args[0]


@pytest.mark.asyncio
async def test_invalid_response_is_preserved_when_a_later_provider_is_busy():
    from aios_app.inference.broker import (
        InferenceBroker, InferenceProviderBusy, InferenceRequest, InferenceUnavailable,
    )
    from aios_app.inference.protocol import StructuredResponseError

    broker = InferenceBroker(MagicMock())
    broker.providers.routable = AsyncMock(return_value=[object(), object()])
    broker._attempt = AsyncMock(side_effect=[StructuredResponseError("malformed JSON"), InferenceProviderBusy("busy")])
    with pytest.raises(InferenceUnavailable) as error:
        await broker.infer(InferenceRequest(instance_id=uuid4(), worker_class="executive", prompt="decide"))
    assert error.value.invalid_response is True
