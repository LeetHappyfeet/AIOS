"""Real SQL isolation and pagination for worker-scoped retained evidence."""
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import test_workspace_client as workspace_tests
from test_character_environments import pair, request
from test_workspace_attachment import attached
from aios_app.agent.environment_links import EnvironmentStore
from aios_app.agent.workspaces import FinishIn, LeaseIn, install_workspace_routes
from aios_app.agent.work_history import worker_work, WorkerWorkResultQuery, WorkerWorkHistoryQuery

db = workspace_tests.db


@pytest.fixture(autouse=True)
def scene(monkeypatch):
    from aios_app.agent.lifecycle import CharacterAgencyStore
    monkeypatch.setattr(CharacterAgencyStore, '_refresh_scene', AsyncMock())


async def prepared(db, attachment=False):
    if attachment:
        store, _, origin, bid, lid, _ = await attached(db)
    else:
        store, origin, _, bid, lid = await pair(db, share=False)
        await db.execute('CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY, superseded_at timestamptz)')
        await db.execute('INSERT INTO aios.ingest_event VALUES(1,NULL)')
    old = await EnvironmentStore(db).delegate(bid, lid, await request(db, origin))
    claim = await store.claim(bid, 'worker', 3)
    prior = UUID(old['run_id'])
    await store.finish(bid, prior, FinishIn(worker_key='worker', claim_token=claim['claim_token'],
        status='finished', result={'report': 'Network probe log\n' + 'evidence\n' * 100,
        'result_summary': {'outcome': 'partial', 'summary': 'HTTPS succeeded to tested destinations',
                           'findings': ['curl exit 0'], 'limitations': ['IPv6 not tested']}}))
    current = await EnvironmentStore(db).delegate(bid, lid, await request(db, origin))
    claim = await store.claim(bid, 'worker', 3)
    lease = LeaseIn(worker_key='worker', claim_token=claim['claim_token'])
    return origin, bid, lid, prior, UUID(current['run_id']), lease


@pytest.mark.asyncio
@pytest.mark.parametrize('attachment', [False, True])
async def test_history_and_reader_work_without_personal_memory_sharing(db, attachment):
    origin, bid, lid, prior, active, lease = await prepared(db, attachment)
    assert await db.fetchval('SELECT share_memory FROM aios.character_environment_link WHERE link_id=$1', lid) is False
    history = await worker_work(db, bid, active, lease)
    assert [r['run_id'] for r in history['runs']] == [str(prior)]
    item = history['runs'][0]
    assert item['summary'] == 'HTTPS succeeded to tested destinations'
    assert item['limitations'] == ['IPv6 not tested'] and item['not_goal_completion']
    assert 'claim_token' not in json.dumps(history) and 'root_hint' not in json.dumps(history)
    query = WorkerWorkResultQuery(**lease.model_dump(), run_id=prior, section='report', limit=100)
    page = await worker_work(db, bid, active, query, read=True)
    whole = page['text']
    while page['next_cursor'] is not None:
        query = query.model_copy(update={'cursor': page['next_cursor'], 'revision': page['revision']})
        page = await worker_work(db, bid, active, query, read=True)
        whole += page['text']
    assert whole == 'Network probe log\n' + 'evidence\n' * 100
    with pytest.raises(HTTPException, match='changed'):
        await worker_work(db, bid, active, query.model_copy(update={'revision': '0' * 64}), read=True)
    with pytest.raises(HTTPException, match='No work result'):
        await worker_work(db, bid, active, query.model_copy(update={'run_id': active}), read=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['binding', 'link', 'origin', 'timeline', 'superseded', 'branch'])
async def test_prior_reports_are_hidden_outside_scope(db, change):
    origin, bid, lid, prior, active, lease = await prepared(db)
    if change == 'binding':
        _, _, _, foreign_bid, _ = await pair(db, share=False)
        await db.execute('UPDATE aios.workspace_run SET binding_id=$2 WHERE run_id=$1', prior, foreign_bid)
    elif change == 'link':
        await db.execute('UPDATE aios.workspace_run SET environment_link_id=NULL WHERE run_id=$1', prior)
    elif change in {'origin', 'timeline'}:
        _, foreign, _, _, _ = await pair(db, share=False)
        if change == 'origin':
            await db.execute('UPDATE aios.workspace_run SET origin_instance_id=$2 WHERE run_id=$1', prior, foreign.instance_id)
        else:
            timeline = await db.fetchval('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', foreign.instance_id)
            await db.execute('UPDATE aios.workspace_run SET origin_source_timeline_id=$2 WHERE run_id=$1', prior, timeline)
    elif change == 'superseded':
        await db.execute('UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=1')
    else:
        sibling = uuid4()
        timeline = await db.fetchval('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
        await db.execute('INSERT INTO aios.dag_node VALUES($1,$2,1)', sibling, timeline)
        await db.execute('UPDATE aios.workspace_run SET origin_source_node_id=$2 WHERE run_id=$1', prior, sibling)
    assert (await worker_work(db, bid, active, lease))['runs'] == []
    with pytest.raises(HTTPException, match='No work result'):
        await worker_work(db, bid, active, WorkerWorkResultQuery(**lease.model_dump(), run_id=prior), read=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revoked', 'expired', 'cancelled', 'foreign_claim', 'origin_branch', 'execution_branch'])
async def test_worker_authority_is_checked_for_every_read(db, change):
    origin, bid, lid, prior, active, lease = await prepared(db)
    if change == 'revoked':
        await EnvironmentStore(db).revoke(bid, lid)
    elif change == 'expired':
        await db.execute("UPDATE aios.workspace_run SET lease_expires_at=now()-interval '1 second' WHERE run_id=$1", active)
    elif change == 'cancelled':
        await EnvironmentStore(db).cancel(bid, active)
    elif change == 'foreign_claim':
        lease = lease.model_copy(update={'claim_token': uuid4()})
    elif change == 'execution_branch':
        execution = await db.fetchval('SELECT instance_id FROM aios.workspace_binding WHERE binding_id=$1', bid)
        await db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1', execution, uuid4())
    else:
        node = uuid4()
        timeline = await db.fetchval('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
        await db.execute('INSERT INTO aios.dag_node VALUES($1,$2,1)', node, timeline)
        await db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1', origin.instance_id, node)
    with pytest.raises(HTTPException):
        await worker_work(db, bid, active, lease)
    with pytest.raises(HTTPException):
        await worker_work(db, bid, active, WorkerWorkResultQuery(**lease.model_dump(), run_id=prior), read=True)


def test_reader_contract_and_routes_require_lease(monkeypatch):
    lease = {'worker_key': 'worker', 'claim_token': uuid4(), 'run_id': uuid4()}
    for extra in ({'instance_id': uuid4()}, {'cursor': 100}, {'limit': 4001}):
        with pytest.raises(ValidationError):
            WorkerWorkResultQuery(**lease, **extra)
    with pytest.raises(ValidationError):
        WorkerWorkHistoryQuery(worker_key='worker', claim_token=uuid4(), instance_id=uuid4())
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    monkeypatch.setenv('AIOS_WORKSPACE_API_KEY', 'worker-key')
    with TestClient(app) as client:
        base = f'/workspace-client/v3/{uuid4()}/runs/{uuid4()}'
        for suffix in ('work-history', 'work-result'):
            assert client.post(base + '/' + suffix, json={}).status_code == 401
            assert client.post(base + '/' + suffix, json={}, headers={'Authorization': 'Bearer worker-key'}).status_code == 422
        assert 'work-evidence-v1' in client.get('/workspace-client/protocol', headers={'Authorization': 'Bearer worker-key'}).json()['capabilities']
