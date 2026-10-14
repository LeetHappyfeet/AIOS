import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aios_app.agent.autonomy import AutonomyScheduler
from aios_app.agent.runtime import AgentRuntimeStore


class WakeDB:
    def __init__(self, event):
        self.event = event
        self.executed = []

    async def fetch(self, sql, *args):
        return [self.event]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def wake(kind, payload):
    return {"wake_id": uuid4(), "event_type": kind,
            "source_type": "dag_node", "source_id": str(uuid4()),
            "payload": json.dumps(payload), "priority": 150}


@pytest.mark.asyncio
async def test_delta_json_payload_preserves_source(monkeypatch):
    from aios_app.agent.opportunity_router import OpportunityRouter
    source = uuid4()
    db = WakeDB(wake("COGNITIVE_DELTA_READY", {"source_through_node_id": str(source)}))
    admit = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(OpportunityRouter, "admit", admit)
    scheduler = AutonomyScheduler(db)
    scheduler.runtime.finish_semantic_turn = AsyncMock()
    assert await scheduler._schedule_instance(uuid4())
    assert admit.await_args.kwargs["source_node_id"] == source
    assert "status='consumed'" in db.executed[0][0]


@pytest.mark.asyncio
async def test_active_goal_json_wake_is_checked_not_discarded():
    goal, trigger = uuid4(), uuid4()
    db = WakeDB(wake("GOAL_REVIEW_DUE", {"goal_id": str(goal), "trigger_id": str(trigger)}))
    db.fetchval = AsyncMock(return_value=1)
    scheduler = AutonomyScheduler(db)
    scheduler.agency.create_task = AsyncMock(side_effect=RuntimeError("routing failed"))
    with pytest.raises(RuntimeError, match="routing failed"):
        await scheduler._schedule_instance(uuid4())
    assert db.fetchval.await_args.args[-1] == goal
    assert not any("SET status='consumed'" in sql for sql, _ in db.executed)
    assert json.loads(db.executed[-1][1][1])["scheduling_retry_attempt"] == 1


@pytest.mark.asyncio
async def test_child_completion_json_resumes_existing_parent():
    parent, child = uuid4(), uuid4()
    db = WakeDB(wake("COGNITIVE_TASK_COMPLETED", {
        "parent_task_id": str(parent), "task_id": str(child)}))
    scheduler = AutonomyScheduler(db)
    scheduler.agency.get_task = AsyncMock(return_value=SimpleNamespace(status="waiting"))
    scheduler.agency.transition_task = AsyncMock()
    assert await scheduler._schedule_instance(uuid4())
    scheduler.agency.transition_task.assert_awaited_once_with(parent, "queued")
    assert any("character_cognitive_task_dependency" in sql for sql, _ in db.executed)


@pytest.mark.asyncio
@pytest.mark.parametrize("previous,exhausted,delay", [(0, False, 2), (2, False, 8), (3, True, 16)])
async def test_failed_wake_is_deferred_then_cancelled(previous, exhausted, delay):
    db = WakeDB(wake("COGNITIVE_DELTA_READY", {"scheduling_retry_attempt": previous}))
    scheduler = AutonomyScheduler(db)
    scheduler._route_events = AsyncMock(side_effect=RuntimeError("serialization failure"))
    with pytest.raises(RuntimeError):
        await scheduler._schedule_instance(uuid4())
    _, args = db.executed[-1]
    payload = json.loads(args[1])
    assert payload["scheduling_retry_attempt"] == previous + 1
    assert payload["scheduling_exhausted"] is exhausted
    assert args[2:] == (exhausted, delay)


@pytest.mark.asyncio
async def test_one_failed_instance_does_not_starve_next():
    first, second = uuid4(), uuid4()
    db = SimpleNamespace(fetch=AsyncMock(side_effect=[[], [{"instance_id": first}, {"instance_id": second}]]))
    scheduler = AutonomyScheduler(db)
    scheduler._enqueue_existing_tasks = AsyncMock(return_value=0)
    scheduler._schedule_instance = AsyncMock(side_effect=[RuntimeError("bad instance"), True])
    assert await scheduler.schedule_ready() == 1
    assert scheduler._schedule_instance.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("previous,expected", [(0, 1), (1, 2), (2, 3), (3, None)])
async def test_rearm_uses_persisted_episode_retry_count(previous, expected):
    db = SimpleNamespace(fetchval=AsyncMock(return_value=previous),
                         execute=AsyncMock(), execute_returning_row=AsyncMock(return_value={"wake_id": uuid4()}))
    result = await AgentRuntimeStore(db).rearm_cognitive_delta(instance_id=uuid4(), source_node_id=uuid4())
    if expected is None:
        assert result is None
        db.execute_returning_row.assert_not_awaited()
    else:
        args = db.execute_returning_row.await_args.args
        assert args[-1] == expected
        assert json.loads(args[3])["retry_attempt"] == expected


@pytest.mark.asyncio
async def test_transactions_run_when_autonomy_admission_fails(monkeypatch):
    from aios_app.supervisor import _schedule_agent_work
    from aios_app.agent.temporal import TemporalTriggerStore
    from aios_app.agent.transaction_scheduler import TransactionScheduler
    monkeypatch.setattr(TemporalTriggerStore, "reset_stale_firing", AsyncMock())
    monkeypatch.setattr(TemporalTriggerStore, "emit_due", AsyncMock())
    monkeypatch.setattr(AgentRuntimeStore, "emit_due_heartbeats", AsyncMock())
    monkeypatch.setattr(AutonomyScheduler, "schedule_ready", AsyncMock(side_effect=RuntimeError("failed")))
    run_pending = AsyncMock()
    monkeypatch.setattr(TransactionScheduler, "run_pending", run_pending)
    await _schedule_agent_work(SimpleNamespace())
    run_pending.assert_awaited_once_with(limit=8)


@pytest.mark.asyncio
async def test_stranded_recovery_failure_does_not_block_ready_instance():
    broken, ready = uuid4(), uuid4()
    db = SimpleNamespace(fetch=AsyncMock(side_effect=[
        [{"instance_id": broken, "pending_cognitive_source_node_id": uuid4()}],
        [{"instance_id": ready}],
    ]))
    scheduler = AutonomyScheduler(db)
    scheduler.runtime.rearm_cognitive_delta = AsyncMock(side_effect=RuntimeError("recovery failed"))
    scheduler._enqueue_existing_tasks = AsyncMock(return_value=0)
    scheduler._schedule_instance = AsyncMock(return_value=True)
    assert await scheduler.schedule_ready() == 1
    scheduler._schedule_instance.assert_awaited_once_with(ready)


@pytest.mark.asyncio
async def test_non_object_payload_is_recorded_for_retry():
    db = WakeDB(wake("COGNITIVE_DELTA_READY", ["bad payload"]))
    scheduler = AutonomyScheduler(db)
    with pytest.raises(ValueError, match="JSON object"):
        await scheduler._schedule_instance(uuid4())
    payload = json.loads(db.executed[-1][1][1])
    assert payload["scheduling_retry_attempt"] == 1
    assert "JSON object" in payload["scheduling_error"]
