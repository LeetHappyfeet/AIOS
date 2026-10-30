"""Pairing diagnostics use the same SQL records as admission and the HUD."""
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_workspace_client as workspace_tests
from test_work_admission import setup
from aios_app.agent.workspace_setup import WorkspaceSetup, ExecutionIn
from aios_app.agent.workspaces import install_workspace_routes
from aios_app.hud.render_text import render_hud_text

db = workspace_tests.db


@pytest.mark.asyncio
async def test_readiness_distinguishes_policy_worker_profile_and_pairing(db, monkeypatch):
    from aios_app.agent.lifecycle import CharacterAgencyStore
    monkeypatch.setattr(CharacterAgencyStore, '_refresh_scene', AsyncMock())
    work, origin, execution, bid, lid, source, req, profile = await setup(db, allow=False, online=False)
    service = WorkspaceSetup(db)
    report = await service.readiness(origin.instance_id, bid)
    e = report['environments'][0]
    assert report['configured'] and not report['ready']
    assert e['approval_required'] and not e['worker_online']
    assert '45 seconds' in e['problems'][0]
    await db.execute("UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1", bid)
    assert (await service.readiness(origin.instance_id, bid))['ready']
    await db.execute("INSERT INTO aios.character_action_policy VALUES($1,'workspace.run','deny')", execution.instance_id)
    assert not (await service.readiness(origin.instance_id, bid))['configured']
    overlay = await work.overlay(origin.instance_id, await db.fetchval(
        'SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id))
    assert not overlay['capabilities'] and 'policy' in overlay['unavailable_reason']
    assert 'WORK UNAVAILABLE:' in render_hud_text({'work': overlay})
    await db.execute("DELETE FROM aios.character_action_policy")
    await db.execute("UPDATE aios.workspace_work_profile SET enabled=false WHERE profile_id=$1", UUID(profile['profile_id']))
    assert 'No enabled work profile' in (await service.readiness(origin.instance_id, bid))['environments'][0]['problems'][0]
    await db.execute("UPDATE aios.character_runtime_state SET source_timeline_id=$2 WHERE instance_id=$1", origin.instance_id, uuid4())
    assert any('Origin conversation changed' in p for p in (await service.readiness(origin.instance_id, bid))['environments'][0]['problems'])


@pytest.mark.asyncio
async def test_missing_link_and_same_instance_explain_incomplete_setup(db):
    store, origin, bid = await workspace_tests.binding(db)
    report = await WorkspaceSetup(db).readiness(origin.instance_id, bid)
    assert not report['configured'] and not report['ready']
    assert any('conversational instance' in p for p in report['problems'])
    from aios_app.agent.work_admission import WorkAdmission
    timeline = await db.fetchval('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
    overlay = await WorkAdmission(db).overlay(origin.instance_id, timeline)
    assert 'No execution environment' in overlay['unavailable_reason']


def test_discovery_and_setup_routes_keep_authority_separate(monkeypatch):
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    monkeypatch.setenv('AIOS_WORKSPACE_API_KEY', 'worker')
    monkeypatch.setenv('AIOS_WORKSPACE_REVIEW_KEY', 'operator')
    with TestClient(app) as client:
        for path in ('characters', 'sessions?character_id=Cortana', 'v3/readiness?origin_instance_id=' + str(uuid4())):
            assert client.get('/workspace-client/' + path).status_code == 401
        assert client.post('/workspace-client/v3/execution-instances', json={'origin_instance_id': str(uuid4())}, headers={'Authorization': 'Bearer worker'}).status_code == 401
        monkeypatch.setattr(WorkspaceSetup, 'characters', AsyncMock(return_value=[{'character_id': 'Cortana', 'display_name': 'Cortana'}]))
        assert client.get('/workspace-client/characters', headers={'Authorization': 'Bearer worker'}).json()['characters'][0]['character_id'] == 'Cortana'


@pytest.mark.asyncio
async def test_execution_creation_uses_new_session_and_preserves_routing(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from aios_app.world.runtime import WorldRuntimeService
    from aios_app import dag, ingest_api
    origin_id, execution_id, world_id, new_world, session_id, source = [uuid4() for _ in range(6)]
    origin = dict(instance_id=origin_id, character_id='Character', user_name='human', scope_key='private', world_id=world_id)
    execution = dict(origin, instance_id=execution_id, world_id=new_world, session_id=session_id)
    @asynccontextmanager
    async def atomic():
        yield
    db = SimpleNamespace(atomic=atomic, fetchval=AsyncMock(return_value=session_id), execute=AsyncMock())
    service = WorkspaceSetup(db)
    service.origin = AsyncMock(side_effect=[origin, execution])
    timeline = AsyncMock(return_value=source)
    activate = AsyncMock(return_value=SimpleNamespace(instance_id=execution_id, world_id=new_world))
    observe = AsyncMock(return_value=SimpleNamespace(timeline_id=source, source_current=True))
    monkeypatch.setattr(ingest_api, 'ingest_message', observe)
    monkeypatch.setattr(dag, 'get_or_create_timeline', timeline)
    monkeypatch.setattr(WorldRuntimeService, 'activate_character', activate)
    result = await service.create_execution(ExecutionIn(origin_instance_id=origin_id))
    assert result['instance_id'] != str(origin_id)
    assert result['world_id'] != str(world_id)
    assert timeline.await_args.kwargs['session_id'] == session_id
    assert activate.await_args.kwargs == dict(character_id='Character', user_name='human', scope_key='private', session_id=session_id)
    assert db.execute.await_args.args[1:] == (execution_id, source)
    assert observe.await_args.args[1].speaker_type == 'tool'
    assert observe.await_args.args[1].payload['source'] == 'aios-workspace-setup'


@pytest.mark.asyncio
async def test_memory_controls_reconcile_and_refuse_isolated_policy(db, monkeypatch):
    from aios_app.agent.environment_links import EnvironmentStore, MemorySharingIn
    from test_character_environments import pair
    import aios_app.epistemic.belief_reconciliation as reconciliation
    monkeypatch.setattr(reconciliation, 'reconcile_instance_beliefs', AsyncMock())
    monkeypatch.setattr(reconciliation, 'reconcile_character_belief_atom', AsyncMock())
    _, origin, execution, bid, lid = await pair(db, share=False)
    env = EnvironmentStore(db)
    await env.set_memory(bid, lid, MemorySharingIn(share_memory=True))
    assert await db.fetchval('SELECT share_memory FROM aios.character_environment_link WHERE link_id=$1', lid)
    assert reconciliation.reconcile_instance_beliefs.await_count == 2
    await env.set_memory(bid, lid, MemorySharingIn(share_memory=False))
    assert not await db.fetchval('SELECT share_memory FROM aios.character_environment_link WHERE link_id=$1', lid)
    await db.execute("INSERT INTO aios.character_epistemic_profile VALUES('Cortana','isolated') ON CONFLICT(character_id) DO UPDATE SET memory_continuity='isolated'")
    from fastapi import HTTPException
    with pytest.raises(HTTPException, match='Isolated'):
        await env.set_memory(bid, lid, MemorySharingIn(share_memory=True))


@pytest.mark.asyncio
async def test_shared_runtime_world_is_rejected(db):
    from test_character_environments import pair
    from aios_app.agent.environment_links import EnvironmentStore
    from fastapi import HTTPException
    _, origin, execution, bid, lid = await pair(db, share=False)
    await db.execute('UPDATE aios.character_instance SET world_id=(SELECT world_id FROM aios.character_instance WHERE instance_id=$1) WHERE instance_id=$2', origin.instance_id, execution.instance_id)
    async with db.atomic():
        link = await EnvironmentStore(db).link(bid, lid)
        with pytest.raises(HTTPException, match='independent world'):
            await EnvironmentStore(db).origin(link)
    report = await WorkspaceSetup(db).readiness(origin.instance_id, bid)
    assert not report['configured']
    assert 'independent world' in report['environments'][0]['problems'][0]
