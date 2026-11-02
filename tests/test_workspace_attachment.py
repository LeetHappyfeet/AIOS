"""Attached work coexists with chat without taking ownership of its source cursor."""
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
import test_workspace_client as workspace_tests
from test_workspace_client import binding, valid_evidence
from aios_app.agent.environment_links import EnvironmentStore, LinkIn, ProgressIn, MemorySharingIn, task_overlay
from aios_app.agent.workspaces import AssignmentIn, ObservationIn, LeaseIn, FinishIn, install_workspace_routes
from aios_app.agent.workspace_contract import Assignment
from aios_app.agent.workspace_setup import WorkspaceSetup
from aios_app.agent.work_admission import WorkAdmission, ProfileIn, WorkRequestIn, WorkDecision
from aios_app.hud.render_text import render_hud_text

db = workspace_tests.db


@pytest.fixture(autouse=True)
def scene(monkeypatch):
    from aios_app.agent.lifecycle import CharacterAgencyStore
    monkeypatch.setattr(CharacterAgencyStore, '_refresh_scene', AsyncMock())


async def attached(db):
    store, origin, bid = await binding(db)
    state = await db.fetchrow('SELECT * FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
    await db.execute("CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY,viewpoint_id text,superseded_at timestamptz)")
    await db.execute("ALTER TABLE aios.dag_node ADD COLUMN speaker_role text,ADD COLUMN speaker_id text,ADD COLUMN message_text text")
    await db.execute("INSERT INTO aios.ingest_event VALUES(1,'Cortana',NULL)")
    await db.execute("INSERT INTO aios.dag_node VALUES($1,$2,1,'character','Cortana','Please write a greeting')", state['source_head_node_id'], state['source_timeline_id'])
    link = await EnvironmentStore(db).create(bid, LinkIn(origin_instance_id=origin.instance_id))
    work = WorkAdmission(db)
    await work.configure(bid, UUID(link['link_id']), ProfileIn(capability='workspace_test', purpose='hello', description='Write a greeting file',
        assignment_template={'required_checks': [{'id':'greeting', 'command':'python3 check_greeting.py'}]}))
    await db.execute('UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1', bid)
    return store, work, origin, bid, UUID(link['link_id']), state


async def chat_turn(db, origin, state):
    node = uuid4()
    await db.execute("INSERT INTO aios.dag_node VALUES($1,$2,1,'character','Cortana','Continue')", node, state['source_timeline_id'])
    await db.execute('INSERT INTO aios.dag_edge VALUES($1,$2,$3)', state['source_timeline_id'], node, state['source_head_node_id'])
    await db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1', origin.instance_id, node)
    return node


@pytest.mark.asyncio
async def test_attached_admission_approval_execution_and_hud_preserve_chat_source(db, monkeypatch):
    import aios_app.ingest_api as ingest
    ingest_spy = AsyncMock(side_effect=AssertionError('Attached worker must not ingest into chat'))
    monkeypatch.setattr(ingest, 'ingest_message', ingest_spy)
    store, work, origin, bid, lid, state = await attached(db)
    assert (await WorkspaceSetup(db).readiness(origin.instance_id, bid))['ready']
    req = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'], capability='workspace_test', purpose='hello', objective='Write greeting file')
    receipt = await work.request(origin.instance_id, req, character_source=True)
    assert receipt['status'] == 'awaiting_approval'
    node = await chat_turn(db, origin, state)
    receipt = await work.decide(origin.instance_id, req.request_id, WorkDecision(accept=True, reviewer='operator', reason='Greeting test'))
    assert receipt['status'] == 'queued'
    assert await store.claim(bid, 'worker', 2) is None
    claimed = await store.claim(bid, 'worker', 3)
    rid = UUID(claimed['run_id'])
    lease = LeaseIn(worker_key='worker', claim_token=claimed['claim_token'])
    await store.renew(bid, rid, lease)
    hud = AsyncMock(return_value={'hud':'task context'})
    app = FastAPI(); install_workspace_routes(app, db, hud)
    context = next(r.endpoint for r in app.routes if getattr(r,'path',None) == '/workspace-client/v3/{binding_id}/runs/{run_id}/context')
    await context(bid, rid, lease)
    assert hud.await_args.kwargs['through_node_id'] == node
    assert hud.await_args.kwargs['profile_name'] == 'workspace_execution'
    observation = ObservationIn(**lease.model_dump(), event_key=uuid4(), text='Greeting file written', payload={'kind':'summary'})
    result = await store.observe(bid, rid, observation)
    assert result['source'] == 'workspace-run' and not result['source_current']
    assert await store.observe(bid, rid, observation) == result
    with pytest.raises(HTTPException, match='different content'):
        await store.observe(bid, rid, observation.model_copy(update={'text':'Changed'}))
    await EnvironmentStore(db).progress(bid, rid, ProgressIn(**lease.model_dump(), event_key=uuid4(), kind='progress', summary='Checking file'))
    current = await db.fetchrow('SELECT * FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
    assert dict(current) == dict(state, source_head_node_id=node)
    assert (await store.binding(bid))['last_node_id'] == state['source_head_node_id']
    spec = Assignment.model_validate(claimed['assignment'])
    finished = await store.finish(bid, rid, FinishIn(**lease.model_dump(), status='succeeded', result={'report':'Greeting verified', 'evidence':valid_evidence(spec)}))
    assert finished['result']['verification_status'] == 'passed'
    overlay = await work.overlay(origin.instance_id, state['source_timeline_id'])
    text = render_hud_text({'work':overlay, 'remote_tasks':await task_overlay(db, origin.instance_id, state['source_timeline_id'])})
    assert 'WORK CAPABILITIES' in text and 'workspace_test / hello' in text
    assert 'REMOTE WORK' in text and 'passed' in text
    tasks = await task_overlay(db, origin.instance_id, state['source_timeline_id'])
    assert tasks[0]['status'] == 'finished' and tasks[0]['verification_status'] == 'passed'
    assert await db.fetchval('SELECT count(*) FROM aios.character_wake_event WHERE instance_id=$1', origin.instance_id) == 1
    ingest_spy.assert_not_called()


@pytest.mark.asyncio
async def test_attachment_accepts_stale_head_and_context_follows_chat(db, monkeypatch):
    store, work, origin, bid, lid, state = await attached(db)
    node = await chat_turn(db, origin, state)
    # Existing link can be selected repeatedly after ordinary chat activity.
    assert (await EnvironmentStore(db).create(bid, LinkIn(origin_instance_id=origin.instance_id)))['link_id'] == str(lid)
    assert (await WorkspaceSetup(db).readiness(origin.instance_id, bid))['ready']
    hud = AsyncMock(return_value={'hud':'task context'})
    app = FastAPI(); install_workspace_routes(app, db, hud)
    monkeypatch.setenv('AIOS_WORKSPACE_API_KEY', 'worker-key')
    # Database fixture's loop owns asyncpg; call the route coroutine directly.
    context = next(r.endpoint for r in app.routes if getattr(r,'path',None) == '/workspace-client/{binding_id}/context')
    await context(bid)
    assert hud.await_args.kwargs['through_node_id'] == node
    with pytest.raises(HTTPException):
        await store.submit(bid, AssignmentIn(request_id=uuid4(), objective='Bypass link'))
    with pytest.raises(HTTPException, match='already uses'):
        await EnvironmentStore(db).set_memory(bid, lid, MemorySharingIn(share_memory=True))
    await db.execute('UPDATE aios.character_runtime_state SET source_timeline_id=$2 WHERE instance_id=$1', origin.instance_id, uuid4())
    assert not (await WorkspaceSetup(db).readiness(origin.instance_id, bid))['ready']
    assert not (await work.overlay(origin.instance_id, state['source_timeline_id']))['capabilities']


@pytest.mark.asyncio
async def test_attachment_is_explicit_and_survives_revocation_without_source_writes(db):
    store, origin, bid = await binding(db)
    node = uuid4()
    await db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1', origin.instance_id, node)
    with pytest.raises(HTTPException):
        async with db.atomic():
            await store.current(await store.binding(bid))
    link = await EnvironmentStore(db).create(bid, LinkIn(origin_instance_id=origin.instance_id))
    assert (await store.binding(bid))['attached']
    await EnvironmentStore(db).revoke(bid, UUID(link['link_id']))
    assert (await store.binding(bid))['attached']  # Revocation never restores a chat writer.
    with pytest.raises(HTTPException):
        await store.submit(bid, AssignmentIn(request_id=uuid4(), objective='Bypass revocation'))


@pytest.mark.asyncio
async def test_attachment_refuses_inflight_writer_and_foreign_routing(db):
    store, origin, bid = await binding(db)
    await workspace_tests.enqueue(db, bid, origin.instance_id)
    with pytest.raises(HTTPException, match='Stop existing'):
        await EnvironmentStore(db).create(bid, LinkIn(origin_instance_id=origin.instance_id))
    assert not (await store.binding(bid))['attached']
    await db.execute("UPDATE aios.workspace_run SET status='cancelled' WHERE binding_id=$1", bid)
    await db.execute("UPDATE aios.timeline SET user_name='someone_else' WHERE session_id=$1", origin.session_id)
    with pytest.raises(HTTPException, match='same character and runtime user'):
        await EnvironmentStore(db).create(bid, LinkIn(origin_instance_id=origin.instance_id))
    assert not (await store.binding(bid))['attached']


@pytest.mark.asyncio
async def test_attached_run_fences_branch_rewind_and_revocation(db):
    store, work, origin, bid, lid, state = await attached(db)
    request = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'], capability='workspace_test', purpose='hello', objective='Greeting')
    await work.request(origin.instance_id, request)
    await work.decide(origin.instance_id, request.request_id, WorkDecision(accept=True, reviewer='operator', reason='Test'))
    claim = await store.claim(bid, 'worker', 3)
    rid = UUID(claim['run_id']); lease = LeaseIn(worker_key='worker', claim_token=claim['claim_token'])
    unrelated = uuid4()
    await db.execute("INSERT INTO aios.dag_node VALUES($1,$2,1,'character','Cortana','Other branch')", unrelated, state['source_timeline_id'])
    await db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1', origin.instance_id, unrelated)
    with pytest.raises(HTTPException, match='active conversation branch'):
        await store.renew(bid, rid, lease)
    with pytest.raises(HTTPException, match='active conversation branch'):
        await store.observe(bid, rid, ObservationIn(**lease.model_dump(), event_key=uuid4(), text='Should not publish'))
    await db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1', origin.instance_id, state['source_head_node_id'])
    await EnvironmentStore(db).revoke(bid, lid)
    assert (await store.renew(bid, rid, lease))['cancel_requested']
    with pytest.raises(HTTPException, match='Cancellation requested'):
        await store.observe(bid, rid, ObservationIn(**lease.model_dump(), event_key=uuid4(), text='Should not publish'))
    ended = await store.finish(bid, rid, FinishIn(**lease.model_dump(), status='succeeded', result={}))
    assert ended['status'] == 'cancelled'
    assert not (await work.overlay(origin.instance_id, state['source_timeline_id']))['capabilities']
