"""One operator profile accepts different objectives without weakening admission."""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import test_workspace_client as workspace_tests
from test_workspace_attachment import attached, scene
from aios_app.agent.work_profiles import workspace_task_profile, expand_request_template, research_search_profile, REPORT_PATH
from aios_app.agent.work_admission import WorkRequestIn, WorkDecision, extract_work_intent, fingerprint
from aios_app.hud.render_text import render_hud_text
from aios_app.agent.workspaces import decode

db = workspace_tests.db


def test_report_check_requires_request_specific_structured_evidence(tmp_path):
    profile = workspace_task_profile()
    request_id = uuid4()
    template = expand_request_template(profile.assignment_template, request_id)
    path = template['expected_outputs'][0]['path']
    assert str(request_id) in path and '{request_id}' not in template['required_checks'][0]['command']
    assert profile.assignment_template['expected_outputs'][0]['path'] == REPORT_PATH
    command = shlex.split(template['required_checks'][0]['command'])
    def check():
        return subprocess.run(command, cwd=tmp_path, capture_output=True).returncode
    assert check() != 0
    (tmp_path / path).write_text('{}')
    assert check() != 0
    (tmp_path / path).write_text(json.dumps({'objective':'Inspect runtime', 'outcome':'blocked', 'evidence':[], 'limitations':['Missing dependency']}))
    assert check() == 0  # Reporting success is distinct from task success.
    other = expand_request_template(profile.assignment_template, uuid4())
    assert other['expected_outputs'][0]['path'] != path
    assert subprocess.run(shlex.split(other['required_checks'][0]['command']), cwd=tmp_path, capture_output=True).returncode != 0


@pytest.mark.asyncio
async def test_same_workspace_task_profile_routes_different_tasks_and_preserves_policies(db):
    store, work, origin, bid, lid, state = await attached(db)
    configured = await work.configure(bid, lid, workspace_task_profile())
    assert (await work.configure(bid, lid, workspace_task_profile())) == configured
    for objective in ('Inspect available runtime and report limitations without making system changes.',
                      'Create a small Python calculator in the workspace and test addition.'):
        req = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'],
                            capability='workspace', purpose='task', objective=objective,
                            constraints=['No sudo'])
        pending = await work.request(origin.instance_id, req, character_source=True)
        assert pending['status'] == 'awaiting_approval'
        assert pending['run_id'] is None
        accepted = await work.decide(origin.instance_id, req.request_id, WorkDecision(accept=True, reviewer='operator', reason='Bounded workspace task'))
        assert accepted['status'] == 'queued'
        run = await db.fetchrow('SELECT * FROM aios.workspace_run WHERE run_id=$1', UUID(accepted['run_id']))
        assignment = decode(run['assignment'])
        assert assignment['objective'].startswith(objective)
        assert 'report structure only' in assignment['objective']
        assert 'No sudo' in assignment['objective']
        assert assignment['budgets'] == {'model_steps':60, 'wall_seconds':1800, 'context_chars':100000}
        assert assignment['expected_outputs'][0]['path'] == f'aios-work-report-{req.request_id}.json'
    assert await db.fetchval('SELECT count(*) FROM aios.workspace_work_profile WHERE profile_id=$1', UUID(configured['profile_id'])) == 1
    await db.execute("INSERT INTO aios.character_action_policy VALUES($1,'workspace.run','deny')", origin.instance_id)
    denied = await work.request(origin.instance_id, req.model_copy(update={'request_id':uuid4(), 'objective':'Another distinct task'}))
    assert denied['status'] == 'denied'


@pytest.mark.asyncio
async def test_hud_uses_real_available_pair_and_mismatch_explains_how_to_correct(db):
    store, work, origin, bid, lid, state = await attached(db)
    await work.configure(bid, lid, workspace_task_profile())
    req = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'],
                        capability='workspace', purpose='invented_purpose', objective='Inspect runtime')
    receipt = await work.request(origin.instance_id, req)
    assert receipt['status'] == 'needs_configuration'
    assert 'workspace / task' in receipt['reason']
    assert 'put the task in objective' in receipt['reason']
    overlay = await work.overlay(origin.instance_id, state['source_timeline_id'])
    rendered = render_hud_text({'work':overlay})
    assert 'listed_capability' not in rendered and 'listed_purpose' not in rendered
    # A HUD advertises multiple tool examples; each character reply still
    # permits only one action. Validate the request example in isolation.
    examples = [json.loads(x) for x in re.findall(r'<aios_action>(.*?)</aios_action>', rendered)]
    example = next(x for x in examples if x.get('op') == 'request_work')
    action = extract_work_intent('<aios_action>' + json.dumps(example) + '</aios_action>')
    assert (action.capability, action.purpose) == ('workspace','task')
    assert 'Use request_work with a listed pair' in rendered
    assert 'experimental_task' not in rendered


@pytest.mark.asyncio
async def test_legacy_profile_migration_preserves_disabled_profile_and_pending_approval(db):
    _, work, origin, bid, lid, state = await attached(db)
    legacy = workspace_task_profile(legacy_report=True)
    configured = await work.configure(bid, lid, legacy)
    req = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'],
                        capability='workspace', purpose='task', objective='Read the workspace inventory',
                        report_mode='inline')
    assert (await work.request(origin.instance_id, req))['status'] == 'awaiting_approval'

    # Simulate a persisted request and preset from the previous release.
    legacy_intent = req.intent().model_dump(mode='json', exclude_none=True)
    legacy_intent['purpose'] = 'experimental_task'
    migration = Path(__file__).parents[1] / 'migrations/current/20261116_01_workspace_tasks.sql'
    async with db.atomic():
        await db.execute('ALTER TABLE aios.character_work_request DISABLE TRIGGER work_request_immutable')
        await db.execute("""UPDATE aios.character_work_request SET intent=$2::jsonb,intent_hash=$3
                         WHERE request_id=$1""", req.request_id, json.dumps(legacy_intent), fingerprint(legacy_intent))
        await db.execute('ALTER TABLE aios.character_work_request ENABLE TRIGGER work_request_immutable')
        await db.execute('ALTER TABLE aios.workspace_work_profile DISABLE TRIGGER work_profile_immutable')
        await db.execute("""UPDATE aios.workspace_work_profile SET purpose='experimental_task', enabled=false
                         WHERE profile_id=$1""", UUID(configured['profile_id']))
        await db.execute('ALTER TABLE aios.workspace_work_profile ENABLE TRIGGER work_profile_immutable')
        await db.execute('ALTER TABLE aios.workspace_registration DROP COLUMN research_ready')
        await db.execute('ALTER TABLE aios.workspace_binding DROP COLUMN research_ready')
        await db.execute('ALTER TABLE aios.workspace_run DROP COLUMN requires_research')
        async with db.connection() as con:
            if os.getenv("AIOS_PGLITE_MODULE"):
                await con.query(migration.read_text(), exec=True)
            else:
                await con.execute(migration.read_text())

    profile = await db.fetchrow('SELECT * FROM aios.workspace_work_profile WHERE profile_id=$1',
                                UUID(configured['profile_id']))
    assert profile['purpose'] == 'task' and not profile['enabled']
    assert decode(profile['assignment_template']) == legacy.assignment_template
    overlay = await work.overlay(origin.instance_id, state['source_timeline_id'])
    assert all(p['purpose'] != 'task' for p in overlay['capabilities'])
    await work.configure(bid, lid, legacy)
    for purpose in ('task', 'experimental_task'):
        retry = req.model_copy(update={'request_id': uuid4(), 'purpose': purpose})
        assert (await work.request(origin.instance_id, retry))['request_id'] == str(req.request_id)
        assert (await work.request(origin.instance_id, retry))['request_id'] == str(req.request_id)
    approved = await work.decide(origin.instance_id, req.request_id,
                                 WorkDecision(accept=True, reviewer='operator', reason='Inventory approved'))
    assert approved['status'] == 'queued'
    assignment = decode(await db.fetchval('SELECT assignment FROM aios.workspace_run WHERE run_id=$1',
                                          UUID(approved['run_id'])))
    assert assignment['expected_outputs'] == [] and assignment['required_checks'] == []


@pytest.mark.asyncio
async def test_research_is_advertised_and_claimed_only_with_ready_listener(db):
    store, work, origin, bid, lid, state = await attached(db)
    await work.configure(bid, lid, research_search_profile())
    timeline = state['source_timeline_id']
    overlay = await work.overlay(origin.instance_id, timeline)
    assert 'research / search' not in render_hud_text({'work': overlay})
    req = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'],
                        capability='research', purpose='search', objective='Search authorized sources')
    assert (await work.request(origin.instance_id, req))['status'] == 'waiting_client'
    await db.execute('UPDATE aios.workspace_binding SET research_ready=true WHERE binding_id=$1', bid)
    overlay = await work.overlay(origin.instance_id, timeline)
    assert 'research / search' in render_hud_text({'work': overlay})
    await work.reconsider(binding_id=bid)
    approved = await work.decide(origin.instance_id, req.request_id,
                                 WorkDecision(accept=True, reviewer='operator', reason='Search approved'))
    assert approved['status'] == 'queued'
    assert await store.claim(bid, 'worker', 3) is None
    claimed = await store.claim(bid, 'worker', 3, research_ready=True)
    assert claimed['run_id'] == approved['run_id']
