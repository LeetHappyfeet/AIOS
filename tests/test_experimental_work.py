"""One operator profile accepts different objectives without weakening admission."""
import json
import shlex
import subprocess
from uuid import UUID, uuid4

import pytest
import test_workspace_client as workspace_tests
from test_workspace_attachment import attached, scene
from aios_app.agent.work_profiles import experimental_profile, expand_request_template, REPORT_PATH
from aios_app.agent.work_admission import WorkRequestIn, WorkDecision, extract_work_intent
from aios_app.hud.render_text import render_hud_text
from aios_app.agent.workspaces import decode

db = workspace_tests.db


def test_report_check_requires_request_specific_structured_evidence(tmp_path):
    profile = experimental_profile()
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
async def test_same_experimental_profile_routes_different_tasks_and_preserves_policies(db):
    store, work, origin, bid, lid, state = await attached(db)
    configured = await work.configure(bid, lid, experimental_profile())
    assert (await work.configure(bid, lid, experimental_profile())) == configured
    for objective in ('Inspect available runtime and report limitations without making system changes.',
                      'Create a small Python calculator in the workspace and test addition.'):
        req = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'],
                            capability='workspace', purpose='experimental_task', objective=objective,
                            constraints=['No sudo'])
        pending = await work.request(origin.instance_id, req, character_source=True)
        assert pending['status'] == 'awaiting_approval'
        assert pending['run_id'] is None
        accepted = await work.decide(origin.instance_id, req.request_id, WorkDecision(accept=True, reviewer='operator', reason='Bounded workspace experiment'))
        assert accepted['status'] == 'queued'
        run = await db.fetchrow('SELECT * FROM aios.workspace_run WHERE run_id=$1', UUID(accepted['run_id']))
        assignment = decode(run['assignment'])
        assert assignment['objective'].startswith(objective)
        assert 'report structure only' in assignment['objective']
        assert 'No sudo' in assignment['objective']
        assert assignment['budgets'] == {'model_steps':60, 'wall_seconds':1800, 'context_chars':100000}
        assert assignment['expected_outputs'][0]['path'] == f'aios-experiment-{req.request_id}.json'
    assert await db.fetchval('SELECT count(*) FROM aios.workspace_work_profile WHERE profile_id=$1', UUID(configured['profile_id'])) == 1
    await db.execute("INSERT INTO aios.character_action_policy VALUES($1,'workspace.run','deny')", origin.instance_id)
    denied = await work.request(origin.instance_id, req.model_copy(update={'request_id':uuid4(), 'objective':'Another distinct task'}))
    assert denied['status'] == 'denied'


@pytest.mark.asyncio
async def test_hud_uses_real_available_pair_and_mismatch_explains_how_to_correct(db):
    store, work, origin, bid, lid, state = await attached(db)
    await work.configure(bid, lid, experimental_profile())
    req = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'],
                        capability='workspace', purpose='invented_purpose', objective='Inspect runtime')
    receipt = await work.request(origin.instance_id, req)
    assert receipt['status'] == 'needs_configuration'
    assert 'workspace / experimental_task' in receipt['reason']
    assert 'put the task in objective' in receipt['reason']
    overlay = await work.overlay(origin.instance_id, state['source_timeline_id'])
    rendered = render_hud_text({'work':overlay})
    assert 'listed_capability' not in rendered and 'listed_purpose' not in rendered
    action = extract_work_intent(rendered)
    assert (action.capability, action.purpose) == ('workspace','experimental_task')
    assert 'do not invent names' in rendered
