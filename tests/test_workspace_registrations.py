"""Standing authorization routes new chats without moving old bindings or memories."""
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import test_workspace_client as fixtures
from test_workspace_client import binding
from aios_app.agent.workspace_registrations import RegistrationStore, RegistrationIn, AuthorizationIn
from aios_app.agent.workspaces import WorkspaceStore, install_workspace_routes
from aios_app.agent.work_admission import WorkAdmission, WorkRequestIn
from aios_app.agent.policy import ActionPolicyService
from aios_app.agent.environment_links import EnvironmentStore

db = fixtures.db


@pytest.fixture(autouse=True)
def scene(monkeypatch):
    from aios_app.agent.lifecycle import CharacterAgencyStore
    monkeypatch.setattr(CharacterAgencyStore, '_refresh_scene', AsyncMock())


async def registration(db, **changes):
    req = RegistrationIn(workspace_key='stable-host', worker_key='worker', character_id='Cortana',
                         user_name='human', scope_key='workspace', root_hint='/project')
    req = req.model_copy(update=changes)
    store = RegistrationStore(db)
    r = await store.register(req)
    assert await store.register(req) == r
    return store, UUID(r['registration_id'])


async def chat(db, *, legacy=False, user='human'):
    _, req, bid = await binding(db)
    if not legacy:
        await db.execute('DELETE FROM aios.workspace_binding WHERE binding_id=$1', bid)
    if user != 'human':
        await db.execute('UPDATE aios.timeline SET user_name=$2 WHERE session_id=$1', req.session_id, user)
    await db.execute('INSERT INTO aios.dag_node(node_id,timeline_id,event_id) VALUES($1,$2,1)',
                     await db.fetchval('SELECT source_head_node_id FROM aios.character_runtime_state WHERE instance_id=$1', req.instance_id),
                     await db.fetchval('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', req.instance_id))
    return req, bid


@pytest.mark.asyncio
async def test_registration_is_not_authorization_and_mismatches_are_explicit(db):
    store, rid = await registration(db)
    origin, _ = await chat(db)
    await store.ensure(origin.instance_id)
    assert await db.fetchval('SELECT count(*) FROM aios.workspace_binding') == 0
    assert (await store.claim(rid, 'worker'))['status'] == 'needs_authorization'
    with pytest.raises(HTTPException, match='root_hint'):
        await store.register(RegistrationIn(workspace_key='stable-host', worker_key='worker', character_id='Cortana', user_name='human', scope_key='workspace', root_hint='/different'))
    with pytest.raises(HTTPException, match='Unknown'):
        await store.claim(rid, 'other-worker')


@pytest.mark.asyncio
async def test_two_new_chats_attach_once_preserve_sources_and_do_not_share_memory(db):
    store, rid = await registration(db)
    await store.authorize(rid, AuthorizationIn(enabled=True))
    first, _ = await chat(db)
    second, _ = await chat(db)
    other, _ = await chat(db, user='other')
    before = [dict(r) for r in await db.fetch('SELECT * FROM aios.character_runtime_state ORDER BY instance_id')]
    for origin in (first, second, other, first):
        await store.ensure(origin.instance_id)
    assert await db.fetchval('SELECT count(*) FROM aios.workspace_binding') == 2
    assert await db.fetchval('SELECT count(*) FROM aios.character_environment_link WHERE enabled AND NOT share_memory') == 2
    assert await db.fetchval('SELECT count(*) FROM aios.workspace_work_profile') == 2
    assert [dict(r) for r in await db.fetch('SELECT * FROM aios.character_runtime_state ORDER BY instance_id')] == before
    for origin in (first, second):
        peers = await db.fetch('SELECT instance_id FROM aios.cognitive_evidence_instances($1)', origin.instance_id)
        assert [r['instance_id'] for r in peers] == [origin.instance_id]
        assert await ActionPolicyService(db).disposition(origin.instance_id, 'workspace.run', 'external_sensitive') == 'require_approval'


@pytest.mark.asyncio
async def test_matching_legacy_adoption_conflict_and_revocation(db):
    store, rid = await registration(db)
    await store.authorize(rid, AuthorizationIn(enabled=True, disposition='allow'))
    origin, bid = await chat(db, legacy=True)
    original = dict(await WorkspaceStore(db).binding(bid))
    await store.ensure(origin.instance_id)
    adopted = await WorkspaceStore(db).binding(bid)
    assert adopted['workspace_key'] == original['workspace_key'] and adopted['registration_id'] == rid
    link = await db.fetchrow('SELECT * FROM aios.character_environment_link WHERE execution_binding_id=$1', bid)
    await EnvironmentStore(db).revoke(bid, link['link_id'])
    with pytest.raises(HTTPException, match='revoked'):
        await store.ensure(origin.instance_id)
    assert not await db.fetchval('SELECT enabled FROM aios.character_environment_link WHERE link_id=$1', link['link_id'])
    other, obid = await chat(db, legacy=True)
    await db.execute("UPDATE aios.workspace_binding SET root_hint='/other' WHERE binding_id=$1", obid)
    with pytest.raises(HTTPException, match='different worker'):
        await store.ensure(other.instance_id)
    assert (await WorkspaceStore(db).binding(obid))['registration_id'] is None
    # Existing policies remain more restrictive than standing allow.
    await db.execute("INSERT INTO aios.character_action_policy VALUES($1,'workspace.run','deny')", origin.instance_id)
    assert await ActionPolicyService(db).disposition(origin.instance_id, 'workspace.run', 'external_sensitive') == 'deny'


@pytest.mark.asyncio
async def test_listener_serializes_chats_and_disable_cancels_work(db):
    store, rid = await registration(db)
    await store.authorize(rid, AuthorizationIn(enabled=True, disposition='allow'))
    origins = [await chat(db), await chat(db)]
    for origin, _ in origins:
        await store.ensure(origin.instance_id)
    await store.claim(rid, 'worker')  # heartbeat
    await db.execute('CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY,viewpoint_id text,superseded_at timestamptz)')
    await db.execute("INSERT INTO aios.ingest_event VALUES(1,'Cortana',NULL)")
    await db.execute('ALTER TABLE aios.dag_node ADD COLUMN speaker_role text,ADD COLUMN speaker_id text,ADD COLUMN message_text text')
    receipts = []
    for origin, _ in origins:
        node = await db.fetchval('SELECT source_head_node_id FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
        receipt = await WorkAdmission(db).request(origin.instance_id, WorkRequestIn(request_id=uuid4(), source_node_id=node,
            capability='workspace', purpose='experimental_task', objective='Inspect Python runtime'))
        assert receipt['status'] == 'queued'
        receipts.append(receipt)
    claimed = await store.claim(rid, 'worker')
    assert claimed['run']['run_id'] == receipts[0]['run_id']
    assert (await store.claim(rid, 'worker'))['status'] == 'busy'
    await store.authorize(rid, AuthorizationIn(enabled=False))
    assert await db.fetchval("SELECT cancel_requested FROM aios.workspace_run WHERE run_id=$1", UUID(receipts[0]['run_id']))
    assert await db.fetchval("SELECT status FROM aios.workspace_run WHERE run_id=$1", UUID(receipts[1]['run_id'])) == 'cancelled'
    assert (await store.claim(rid, 'worker'))['status'] == 'needs_authorization'


@pytest.mark.asyncio
async def test_authorization_conflict_is_not_silently_resolved(db):
    store, first = await registration(db)
    _, second = await registration(db, workspace_key='another-host', root_hint='/other')
    await store.authorize(first, AuthorizationIn(enabled=True))
    with pytest.raises(HTTPException, match='Another workspace'):
        await store.authorize(second, AuthorizationIn(enabled=True))


def test_worker_key_cannot_authorize_registration(monkeypatch):
    monkeypatch.setenv('AIOS_WORKSPACE_API_KEY', 'worker-secret')
    monkeypatch.setenv('AIOS_WORKSPACE_REVIEW_KEY', 'operator-secret')
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    with TestClient(app) as client:
        result = client.post(f'/workspace-client/registrations/{uuid4()}/authorization',
                             headers={'Authorization': 'Bearer worker-secret'}, json={'enabled': True})
        assert result.status_code == 401


def test_registration_route_accepts_sessionless_payload_and_openapi(monkeypatch):
    monkeypatch.setenv('AIOS_WORKSPACE_API_KEY', 'worker-secret')
    register = AsyncMock(return_value={'registration_id': str(uuid4()), 'enabled': False})
    monkeypatch.setattr(RegistrationStore, 'register', register)
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    with TestClient(app) as client:
        response = client.post('/workspace-client/registrations',
            headers={'Authorization': 'Bearer worker-secret'},
            json={'workspace_key': 'stable', 'worker_key': 'worker', 'character_id': 'Cortana',
                  'user_name': 'human', 'root_hint': '/project'})
        assert response.status_code == 200 and not response.json()['enabled']
        assert 'instance_id' not in register.await_args.args[0].model_dump()
        assert '/workspace-client/registrations' in client.get('/openapi.json').json()['paths']


@pytest.mark.asyncio
async def test_hud_auto_attaches_new_chat_and_pending_request_queues_when_listener_returns(db):
    store, rid = await registration(db)
    await store.authorize(rid, AuthorizationIn(enabled=True, disposition='allow'))
    origin, _ = await chat(db)
    state = await db.fetchrow('SELECT * FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
    await db.execute('CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY,viewpoint_id text,superseded_at timestamptz)')
    await db.execute("INSERT INTO aios.ingest_event VALUES(1,'Cortana',NULL)")
    await db.execute('ALTER TABLE aios.dag_node ADD COLUMN speaker_role text,ADD COLUMN speaker_id text,ADD COLUMN message_text text')
    # A request arriving before a HUD fetch provisions its own authorized attachment.
    result = await WorkAdmission(db).request(origin.instance_id, WorkRequestIn(
        request_id=uuid4(), source_node_id=state['source_head_node_id'],
        capability='workspace', purpose='experimental_task', objective='Inspect the runtime'))
    assert result['status'] == 'waiting_client'
    overlay = await WorkAdmission(db).overlay(origin.instance_id, state['source_timeline_id'])
    assert overlay['capabilities'][0]['purpose'] == 'experimental_task'
    assert not overlay['capabilities'][0]['online']
    claimed = await store.claim(rid, 'worker')
    run_id = UUID(claimed['run']['run_id'])
    assert await db.fetchval('SELECT run_id FROM aios.character_work_request WHERE request_id=$1', UUID(result['request_id'])) == run_id
    # A later chat requires no explicit ensure, client rebinding or operator steps.
    later, _ = await chat(db)
    timeline = await db.fetchval('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', later.instance_id)
    overlay = await WorkAdmission(db).overlay(later.instance_id, timeline)
    assert overlay['capabilities'][0]['online']
    assert await db.fetchval('SELECT count(*) FROM aios.workspace_binding WHERE registration_id=$1', rid) == 2
    # An abandoned lease is recorded as interrupted rather than replayed.
    await db.execute("UPDATE aios.workspace_run SET lease_expires_at=now()-interval '1 second' WHERE run_id=$1", run_id)
    assert (await store.claim(rid, 'worker'))['run'] is None
    assert await db.fetchval('SELECT status FROM aios.workspace_run WHERE run_id=$1', run_id) == 'interrupted'
