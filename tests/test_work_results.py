"""Work HUD preserves limitations, scope and lifecycle evidence."""
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

import test_workspace_client as workspace_tests
from test_work_admission import setup, scene
from aios_app.agent.environment_links import task_overlay, append_event
from aios_app.agent.work_admission import WorkDismissal
from aios_app.agent.work_profiles import experimental_profile, expand_request_template
from aios_app.hud.work_results import result_view
from aios_app.agent.workspaces import WorkspaceStore, FinishIn, LeaseIn
from aios_app.agent.workspace_contract import Assignment
from aios_app.hud.render_text import render_hud_text

db = workspace_tests.db


def task(result, assignment=None, **extra):
    return dict(run_id='run-1', request_id='request-1', status='finished',
                objective='Survey environment', verification_status='passed',
                goal_review='not_linked', result_summary=result_view(result, assignment or {}), **extra)


def test_structured_limitations_survive_long_report_and_keep_association():
    template = expand_request_template(experimental_profile().assignment_template, uuid4())
    result = {'report': 'Everything done!\n| Check | Command | Result |\n' + '| value |' * 100,
              'result_summary': {'outcome': 'blocked', 'findings': ['rg unavailable'],
                                 'limitations': ['Network isolation was not verified']},
              'evidence': {'checks': [{'id': 'experiment_report', 'exit_code': 0}]}}
    t = task(result, template)
    frame = {'remote_tasks': [t], 'work': {'requests': [dict(request_id='request-1', run_id='run-1', status='finished')]}}
    text = render_hud_text(frame)
    assert 'Worker-reported outcome: blocked' in text
    assert 'Network isolation was not verified' in text
    assert 'Report structure only; task success is not established' in text
    assert 'Check experiment_report: exit 0' in text
    assert 'run-1; request: request-1' in text
    assert '| Check |' not in text
    assert 'WORK REQUESTS' not in text  # A single connected receipt, not duplicated entries.
    assert 'Everything done!' in text


def test_worker_scope_cannot_upgrade_authority_and_id_alone_is_not_scope():
    assignment = {'required_checks': [{'id': 'experiment_report', 'command': 'echo not-a-report-check'}]}
    view = result_view({'verification_scope': 'All behavior independently verified'}, assignment)
    assert view['verification_scope'].startswith('Configured checks only')
    assert view['worker_verification_scope'] == 'All behavior independently verified'


def test_legacy_report_has_excerpt_marker_without_fabricated_outcome():
    text = render_hud_text({'remote_tasks': [task({'report': 'legacy ' * 100})]})
    assert 'Worker-reported outcome: not supplied' in text
    assert 'Worker report excerpt (unverified)' in text and '…' in text


def test_dangling_run_reference_is_explicit_not_false_pointer():
    text = render_hud_text({'work': {'requests': [dict(request_id='r', run_id='omitted',
                            status='finished', objective='Survey')]}})
    assert 'details omitted from this HUD; do not resubmit' in text
    assert 'See REMOTE WORK' not in text


def test_budget_error_and_breakdown_remain_visible():
    t = task({'error': 'Pinned context exceeds budget', 'context_budget': {
        'limit': 10000, 'assignment': 3000, 'guidance': 5000, 'tools': 4000}})
    t['status'] = 'failed'
    text = render_hud_text({'remote_tasks': [t]})
    assert 'Error: Pinned context exceeds budget' in text
    assert '"limit": 10000' in text and '"tools": 4000' in text


def test_untrusted_malformed_optional_fields_are_bounded():
    result = {'result_summary': {'findings': 'wrong type', 'outcome': {'not': 'text'},
                                 'limitations': ['x' * 1000] * 100},
              'evidence': {'checks': 'wrong type', 'violations': [{'path': 'a', 'reason': 'changed'}]}}
    view = result_view(result, {})
    assert view['findings'] == [] and view['worker_outcome'] == 'not supplied'
    assert len(view['limitations']) == 5
    result['result_summary'].update(findings=['f' * 1000] * 10, artifacts=['a' * 1000] * 10)
    text = render_hud_text({'remote_tasks': [task(result) for _ in range(8)]})
    assert len(text) < 13000 and 'omitted by HUD budget' in text


@pytest.mark.asyncio
async def test_finish_retains_full_result_and_review_does_not_replace_it(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    receipt = await work.request(origin.instance_id, req)
    run = await WorkspaceStore(db).claim(bid, 'worker', 3)
    rid = UUID(run['run_id'])
    lease = LeaseIn(worker_key='worker', claim_token=run['claim_token'])
    spec = Assignment.model_validate(run['assignment'])
    raw = {'report': 'Original execution report ' + 'detail ' * 200,
           'result_summary': {'outcome': 'partial', 'limitations': ['Integration untested'],
                              'findings': ['Addition repaired']},
           'evidence': workspace_tests.valid_evidence(spec)}
    await WorkspaceStore(db).finish(bid, rid, FinishIn(**lease.model_dump(), status='finished', result=raw))
    await append_event(db, rid, 'test:review', 'reviewed', 'Goal evidence accepted')
    timeline = await db.fetchval('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
    tasks = await task_overlay(db, origin.instance_id, timeline, run_ids=[rid])
    text = render_hud_text({'work': await work.overlay(origin.instance_id, timeline), 'remote_tasks': tasks})
    assert 'Original execution report' in text and 'Integration untested' in text
    assert 'Goal evidence accepted' not in text
    assert str(req.request_id) in text and str(rid) in text
    info = await work.inspect(origin.instance_id, req.request_id)
    assert info['execution']['result']['report'] == raw['report']
    assert 'claim_token' not in info['execution']


@pytest.mark.asyncio
async def test_dismissed_configuration_request_never_reconsidered(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    missing = req.model_copy(update={'purpose': 'missing_profile'})
    assert (await work.request(origin.instance_id, missing))['status'] == 'needs_configuration'
    dismiss = WorkDismissal(reviewer='operator', reason='Obsolete experiment')
    closed = await work.dismiss(origin.instance_id, req.request_id, dismiss)
    assert closed['status'] == 'invalidated'
    assert await work.dismiss(origin.instance_id, req.request_id, dismiss) == closed
    await work.configure(bid, lid, experimental_profile().model_copy(update={
        'capability': missing.capability, 'purpose': missing.purpose}))
    await work.reconsider(instance_id=origin.instance_id)
    assert await db.fetchval('SELECT count(*) FROM aios.workspace_run') == 0
    timeline = await db.fetchval('SELECT source_timeline_id FROM aios.character_runtime_state WHERE instance_id=$1', origin.instance_id)
    overlay = await work.overlay(origin.instance_id, timeline)
    assert overlay['requests'] == [] and overlay['history_count'] == 1
    assert 'no retry required' in render_hud_text({'work': overlay})
    assert (await work.inspect(origin.instance_id, req.request_id))['intent']['purpose'] == 'missing_profile'
    with pytest.raises(HTTPException):
        await work.dismiss(execution.instance_id, req.request_id, dismiss)


@pytest.mark.asyncio
async def test_dismiss_cannot_cancel_admitted_execution(db):
    work, origin, execution, bid, lid, source, req, profile = await setup(db)
    await work.request(origin.instance_id, req)
    with pytest.raises(HTTPException, match='already admitted'):
        await work.dismiss(origin.instance_id, req.request_id, WorkDismissal(reviewer='operator', reason='Stop'))
    assert await db.fetchval('SELECT status FROM aios.workspace_run') == 'queued'


def test_dismissal_route_requires_operator_key(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aios_app.agent.workspaces import install_workspace_routes
    app = FastAPI()
    install_workspace_routes(app, object(), None)
    monkeypatch.setenv('AIOS_WORKSPACE_API_KEY', 'worker-key')
    monkeypatch.setenv('AIOS_WORKSPACE_REVIEW_KEY', 'review-key')
    route = f'/workspace-client/work/{uuid4()}/{uuid4()}/dismiss'
    body = {'reviewer': 'operator', 'reason': 'Obsolete'}
    with TestClient(app) as client:
        assert client.post(route, json=body).status_code == 401
        assert client.post(route, json=body, headers={'Authorization': 'Bearer worker-key'}).status_code == 401
