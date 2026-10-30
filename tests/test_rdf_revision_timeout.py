"""A background check has one budget, one session and useful failure diagnostics."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aios_app.entity_memory import retrieval


def context():
    return SimpleNamespace(instance_id=uuid4(), character_id='Renamon',
                           source_head_node_id=uuid4(), source_timeline_id=uuid4())


class DB:
    def __init__(self, source_delay=0, atom_delay=0, failure=None):
        self.delays = [source_delay, atom_delay]
        self.failure = failure
        self.tasks = []
        self.transaction_task = None
        self.exited = False
    @asynccontextmanager
    async def atomic(self):
        self.transaction_task = asyncio.current_task()
        try:
            yield
        finally:
            self.exited = True
    async def fetch(self, sql, *args):
        index = len(self.tasks)
        self.tasks.append(asyncio.current_task())
        assert self.tasks[-1] == self.transaction_task
        await asyncio.sleep(self.delays[index])
        if self.failure:
            raise self.failure
        return []


@pytest.mark.asyncio
async def test_background_budget_increases_without_changing_foreground(monkeypatch):
    assert retrieval.REVISION_SQL_BUDGET == 7.0
    assert retrieval.SQL_BUDGET == .35
    monkeypatch.setattr(retrieval, 'SQL_BUDGET', .005)
    db = DB(.015, .015)
    service = retrieval.RDFRecallService(db)
    assert await service.discovery_revision(context(), 'cookbook') is not None
    assert db.tasks[0] == db.tasks[1] and db.exited
    assert service.discovery_diagnostics['stage'] == 'completed'
    assert service.discovery_diagnostics['sources_ms'] >= 5
    assert service.discovery_diagnostics['atoms_ms'] >= 5


@pytest.mark.asyncio
async def test_one_deadline_covers_both_reads_and_logs_no_timeout_traceback(monkeypatch, caplog):
    monkeypatch.setattr(retrieval, 'REVISION_SQL_BUDGET', .04)
    db = DB(.025, .025)
    service = retrieval.RDFRecallService(db)
    assert await service.discovery_revision(context(), 'cookbook') is None
    assert db.exited and len(db.tasks) == 2
    assert service.discovery_diagnostics['stage'] == 'atoms'
    assert service.discovery_diagnostics['failure'] == 'TimeoutError'
    assert 'sources_ms' in service.discovery_diagnostics
    warning = next(record for record in caplog.records if 'timed out' in record.message)
    assert warning.exc_info is None


@pytest.mark.asyncio
async def test_unexpected_error_keeps_traceback_and_phase(caplog):
    service = retrieval.RDFRecallService(DB(failure=RuntimeError('missing relation')))
    assert await service.discovery_revision(context(), 'cookbook') is None
    assert service.discovery_diagnostics['stage'] == 'sources'
    assert service.discovery_diagnostics['failure'] == 'RuntimeError'
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_revision_cancellation_propagates_and_releases_transaction():
    db = DB(10, 0)
    task = asyncio.create_task(retrieval.RDFRecallService(db).discovery_revision(context(), 'cookbook'))
    while not db.tasks:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.exited


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['sql_sources', 'sql_atoms', 'fuseki_sources', 'sql_revalidation'])
async def test_recall_failure_retains_phase_and_detail(stage, monkeypatch, caplog):
    import logging
    from unittest.mock import AsyncMock
    from aios_app.rdf.fuseki import FusekiError
    service = retrieval.RDFRecallService(None)
    sources = [{}] if stage == 'fuseki_sources' else []
    rows = AsyncMock(side_effect=([RuntimeError('source failure')] if stage == 'sql_sources' else
        [[], RuntimeError('atom failure')] if stage == 'sql_atoms' else [sources, []]))
    monkeypatch.setattr(service, '_rows', rows)
    monkeypatch.setattr(retrieval, 'entity_query', lambda *args: 'SELECT')
    monkeypatch.setattr(service, '_query', AsyncMock(side_effect=FusekiError('HTTP 503 char/sparql unavailable')))
    monkeypatch.setattr(service, 'validate_refs', AsyncMock(side_effect=TimeoutError()))
    with caplog.at_level(logging.INFO):
        result = await service.search(context(), 'kitchen sink', record=False)
    assert result.status == 'unavailable'
    assert result.diagnostics['stage'] == stage
    assert result.diagnostics['sql_budget_seconds'] == retrieval.INQUIRY_SQL_BUDGET
    assert result.diagnostics['elapsed_ms'] >= 0
    assert f'stage={stage}' in caplog.text
    if stage == 'fuseki_sources':
        assert 'HTTP 503' in result.diagnostics['detail']
        assert 'HTTP 503' in caplog.text


@pytest.mark.asyncio
async def test_background_recall_budget_does_not_extend_interactive_reads(monkeypatch):
    from unittest.mock import AsyncMock
    service = retrieval.RDFRecallService(None)
    rows = AsyncMock(return_value=[])
    monkeypatch.setattr(service, '_rows', rows)
    await service.search(context(), 'kitchen sink', consumer='hud', record=False)
    assert all(call.kwargs['sql_budget'] == retrieval.SQL_BUDGET for call in rows.await_args_list)
    rows.reset_mock()
    await service.search(context(), 'kitchen sink', consumer='inquiry', record=False)
    assert all(call.kwargs['sql_budget'] == retrieval.INQUIRY_SQL_BUDGET for call in rows.await_args_list)
