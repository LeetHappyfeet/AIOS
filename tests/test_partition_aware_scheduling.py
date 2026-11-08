"""Protect per-scope serialization without creating lock-deferral churn."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aios_app import runner
from aios_app.pipeline import jobs


@pytest.mark.asyncio
async def test_claim_sql_excludes_running_effective_scopes_across_resource_classes():
    statements = []
    class DB:
        async def execute_returning_row(self, sql, *args):
            statements.append((sql, args))
            return None
    assert await jobs.fetch_next_job(
        DB(), resource_class="SEMANTIC", scheduling_lanes=["STRUCTURAL"]
    ) is None
    sql, args = statements[0]
    assert "NOT EXISTS (" in sql
    assert "active.status='running'" in sql
    assert "active.resource_class || ':' || active.partition_key" in sql
    assert "q.resource_class || ':' || q.partition_key" in sql
    assert "'semantic-scope:' || q.partition_key" in sql
    assert "'semantic-scope:' || active.partition_key" in sql
    assert "q.partition_key IS NULL" in sql
    assert "q.resource_class = $1" in sql
    assert args[0] == "SEMANTIC"


@pytest.mark.asyncio
async def test_rdf_gate_is_acquired_before_partition_lock(monkeypatch):
    events = []
    job_id = uuid4()
    class Gate:
        async def __aenter__(self):
            events.append("rdf-gate-enter")
        async def __aexit__(self, *exc):
            events.append("rdf-gate-exit")
    class Con:
        async def fetchval(self, *args):
            events.append("partition-lock")
            return True
        async def execute(self, *args, **kwargs):
            events.append("partition-unlock")
        def terminate(self):
            events.append("terminate")
    @asynccontextmanager
    async def connection():
        yield Con()
    class DB:
        connection = staticmethod(connection)
        async def execute(self, *args):
            events.append("partition-record")
    async def no_heartbeat(*args, **kwargs):
        await asyncio.Event().wait()
    def handler(*args):
        events.append("handler")
    monkeypatch.setitem(runner.JOB_HANDLERS, "rdf_epistemic_project", handler)
    monkeypatch.setattr(runner, "_run_isolated_handler",
                        lambda *args: events.append("handler"))
    monkeypatch.setattr(runner, "_heartbeat_loop", no_heartbeat)
    monkeypatch.setattr(runner, "_resolve_partition_key",
                        AsyncMock(return_value="char:Renamon"))
    monkeypatch.setattr(runner, "admit_job", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "release_job", AsyncMock())
    monkeypatch.setattr(runner, "mark_done", AsyncMock())
    await runner._execute_claimed_job(
        DB(), job={"job_id": job_id, "job_type": "rdf_epistemic_project"},
        worker_id="benchmark", rdf_gate=Gate(), resource_governor=object()
    )
    assert events.index("rdf-gate-enter") < events.index("partition-lock")
    assert events.index("partition-lock") < events.index("handler")
    assert events.index("handler") < events.index("partition-unlock")
    assert events.index("partition-unlock") < events.index("rdf-gate-exit")


@pytest.mark.asyncio
async def test_rdf_wait_cancellation_does_not_acquire_partition_lock(monkeypatch):
    entered = asyncio.Event()
    class Gate:
        async def __aenter__(self):
            entered.set()
            await asyncio.Event().wait()
        async def __aexit__(self, *exc):
            pass
    class DB:
        @asynccontextmanager
        async def connection(self):
            pytest.fail("partition connection acquired before RDF gate")
            yield
    async def no_heartbeat(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(runner, "_heartbeat_loop", no_heartbeat)
    monkeypatch.setattr(runner, "_resolve_partition_key",
                        AsyncMock(return_value="char:Renamon"))
    monkeypatch.setattr(runner, "admit_job", AsyncMock(return_value=True))
    release = AsyncMock()
    monkeypatch.setattr(runner, "release_job", release)
    job = {"job_id": uuid4(), "job_type": "rdf_epistemic_project"}
    task = asyncio.create_task(runner._execute_claimed_job(
        DB(), job=job, worker_id="benchmark", rdf_gate=Gate(),
        resource_governor=object(),
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.assert_awaited_once()
