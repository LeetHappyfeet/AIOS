import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from aios_app.pipeline.execution import LeaseLost, run_owned
from aios_app.pipeline.jobs import JobDeferred


def test_parked_only_is_caught_up_but_running_work_is_busy():
    from aios_app.runner_v2 import _candidate_state
    idle = dict(queued=0, oldest_s=0, arrivals_per_s=0, done_per_s=0, queue_velocity=0)
    assert _candidate_state(**idle) == 'CAUGHT_UP'
    assert _candidate_state(**idle, running=1) == 'BUSY'


def test_launch_status_explains_parked_jobs(monkeypatch, capsys):
    from aios_app import launch
    monkeypatch.setitem(launch._TELEMETRY, 'pipeline', {
        'state': 'CAUGHT_UP', 'queued': 0, 'running': 0, 'oldest_s': 0,
        'parked': 2, 'delayed': 1, 'waiting': 0,
        'stage_backlog': [{'job_type': 'goal_plan_continuation', 'parked': 2,
                           'queued': 0, 'running': 0, 'resource': 'LOOKUP', 'lane': 'BACKGROUND'}]})
    launch._render_status({})
    text = capsys.readouterr().out
    assert 'GLOBAL CAUGHT_UP 0q/0r lag 0ms' in text
    assert 'parked 2 delayed 1' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_lease_loss_stops_execution_and_waits_for_cleanup(failure):
    started, stopped, lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async def renew():
        await lost.wait()
        if failure:
            raise RuntimeError('heartbeat unavailable')
    heartbeat = asyncio.create_task(renew())
    task = asyncio.create_task(run_owned(work(), heartbeat))
    await started.wait()
    lost.set()
    with pytest.raises(LeaseLost):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_execution_deadline_cancels_before_retry_and_success_keeps_heartbeat_alive():
    stopped = asyncio.Event()
    async def stuck():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    heartbeat = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(JobDeferred, match='deadline'):
            await run_owned(stuck(), heartbeat, timeout=0.01)
        assert stopped.is_set() and not heartbeat.done()
        assert await run_owned(asyncio.sleep(0, result=42), heartbeat) == 42
        assert not heartbeat.done()
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


@pytest.mark.asyncio
async def test_continuation_deadline_covers_pre_discovery_reads(monkeypatch):
    from aios_app.agent.goal_planning import GoalPlanningService
    from aios_app.agent import goal_planning
    stopped = asyncio.Event()
    async def query(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(goal_planning, 'CONTINUATION_SECONDS', 0.01)
    with pytest.raises(JobDeferred, match='continuation deadline'):
        await GoalPlanningService(SimpleNamespace(fetchval=query)).run_continuation(uuid4())
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_private_lookup_pool_is_terminated_on_cancellation(monkeypatch):
    from aios_app import runner
    started = asyncio.Event()
    pool = SimpleNamespace(terminate=lambda: setattr(pool, 'terminated', True), terminated=False)
    db = SimpleNamespace(pool=pool, connect=AsyncMock(), close=AsyncMock())
    def database(*args, **kwargs):
        assert kwargs['max_size'] == 2
        assert kwargs['server_settings']['lock_timeout'] == '750'
        return db
    monkeypatch.setattr(runner, 'Database', database)
    async def handler(db, job):
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(runner._run_lookup_handler(handler, {'job_id': uuid4()}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.terminated
    db.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', [True, False])
async def test_runner_yields_busy_locks_and_releases_governor_after_lease_loss(monkeypatch, busy):
    from aios_app import runner
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=not busy), execute=AsyncMock(), terminate=lambda: None)
    @asynccontextmanager
    async def connection():
        yield conn
    db = SimpleNamespace(connection=connection, execute=AsyncMock())
    handler_started = asyncio.Event()
    stopped = asyncio.Event()
    async def handler(*args):
        handler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async def heartbeat(*args, **kwargs):
        if not busy:
            await handler_started.wait()
        else:
            await asyncio.Event().wait()
    monkeypatch.setitem(runner.JOB_HANDLERS, 'goal_plan_continuation', handler)
    monkeypatch.setattr(runner, '_run_lookup_handler', handler)
    monkeypatch.setattr(runner, '_heartbeat_loop', heartbeat)
    monkeypatch.setattr(runner, '_resolve_partition_key', AsyncMock(return_value='instance:test'))
    monkeypatch.setattr(runner, 'admit_job', AsyncMock(return_value=True))
    release = AsyncMock()
    defer = AsyncMock()
    done = AsyncMock()
    monkeypatch.setattr(runner, 'release_job', release)
    monkeypatch.setattr(runner, 'defer_claimed_job', defer)
    monkeypatch.setattr(runner, 'mark_done', done)
    await runner._execute_claimed_job(db, job={'job_id': uuid4(), 'job_type': 'goal_plan_continuation'},
        worker_id='worker', rdf_gate=asyncio.Semaphore(1), resource_governor=object())
    release.assert_awaited_once()
    defer.assert_awaited_once()
    done.assert_not_awaited()
    assert handler_started.is_set() == (not busy)
    assert stopped.is_set() == (not busy)
    assert 'pg_try_advisory_lock' in conn.fetchval.await_args.args[0]


@pytest.mark.asyncio
async def test_lock_acquisition_cancellation_discards_session(monkeypatch):
    from aios_app import runner
    entered = asyncio.Event()
    async def acquire(*args):
        entered.set()
        await asyncio.Event().wait()
    conn = SimpleNamespace(fetchval=acquire, execute=AsyncMock(), terminate=Mock())
    @asynccontextmanager
    async def connection():
        yield conn
    async def heartbeat(*args, **kwargs):
        await entered.wait()
    db = SimpleNamespace(connection=connection)
    monkeypatch.setitem(runner.JOB_HANDLERS, 'goal_plan_continuation', AsyncMock())
    monkeypatch.setattr(runner, '_heartbeat_loop', heartbeat)
    monkeypatch.setattr(runner, '_resolve_partition_key', AsyncMock(return_value='instance:test'))
    monkeypatch.setattr(runner, 'admit_job', AsyncMock(return_value=True))
    release, defer = AsyncMock(), AsyncMock()
    monkeypatch.setattr(runner, 'release_job', release)
    monkeypatch.setattr(runner, 'defer_claimed_job', defer)
    await runner._execute_claimed_job(db, job={'job_id': uuid4(), 'job_type': 'goal_plan_continuation'},
        worker_id='worker', rdf_gate=asyncio.Semaphore(1), resource_governor=object())
    conn.terminate.assert_called_once()
    conn.execute.assert_not_awaited()
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_telemetry_counts_pending_once_and_reuses_rate_sample(monkeypatch):
    from aios_app import runner_v2
    from aios_app.pipeline.telemetry import STAGES_SQL
    stages = [dict(job_type='goal_plan_continuation', resource_class='LOOKUP', scheduling_lane='BACKGROUND',
                   queued=0, running=0, parked=2, delayed=0, waiting=0, oldest_seconds=0)]
    async def fetch(sql):
        return stages if sql == STAGES_SQL else []
    rates = AsyncMock(return_value={})
    db = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), fetch=fetch, fetchrow=rates)
    def database(*args, **kwargs):
        assert kwargs['server_settings']['statement_timeout'] == '2000'
        return db
    monkeypatch.setattr(runner_v2, 'Database', database)
    packets = []
    def emit(packet):
        packets.append(packet)
        if len(packets) == 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(runner_v2, '_emit_telemetry', emit)
    with pytest.raises(asyncio.CancelledError):
        await runner_v2._pipeline_telemetry_loop(interval=0)
    rates.assert_awaited_once()
    db.close.assert_awaited_once()
    assert all(p['queued'] == 0 and p['parked'] == 2 and p['oldest_s'] == 0 for p in packets)
