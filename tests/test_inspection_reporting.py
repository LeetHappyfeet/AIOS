from copy import deepcopy
from uuid import UUID, uuid4

import pytest
import test_workspace_client as workspace_tests
from test_workspace_attachment import attached, scene
from aios_app.agent.work_admission import WorkRequestIn, WorkDecision
from aios_app.agent.work_profiles import experimental_profile, reporting_template
from aios_app.agent.workspace_contract import requires_read_only
from aios_app.agent.workspaces import decode
from aios_app.hud.work_results import result_view
from aios_app.hud.work import render_work_results

db = workspace_tests.db


def test_builtin_readonly_reporting_has_no_file_or_shell_requirements():
    template = experimental_profile().assignment_template
    before = deepcopy(template)
    readonly, guidance = reporting_template(template, read_only=True)
    assert readonly['expected_outputs'] == [] and readonly['required_checks'] == []
    assert 'Return the findings directly' in guidance
    assert template == before  # Existing writable profiles retain their checks.
    writable, guidance = reporting_template(template, read_only=False)
    assert writable == before and 'write the expected JSON report file' in guidance


def test_readonly_cannot_bypass_custom_operator_checks():
    template = experimental_profile().assignment_template
    template['required_checks'][0]['command'] = 'custom operator check'
    with pytest.raises(ValueError, match='custom profile'):
        reporting_template(template, read_only=True)
    configured, _ = reporting_template({'expected_outputs': [], 'required_checks': []}, read_only=True)
    assert configured['required_checks'] == []


def test_failure_reason_and_response_diagnostics_precede_partial_findings():
    result = {'error': 'Model returned neither tool calls nor a final report',
        'model_diagnostics': {'finish_reason': 'length', 'max_tokens': 2048,
                              'usage': {'completion_tokens': 2048}},
        'result_summary': {'outcome': 'partial', 'summary': 'Retained tool receipts',
                           'findings': ['Python 3.10.12 observed']}}
    view = result_view(result, {})
    task = {'run_id': 'run', 'status': 'failed', 'result_summary': view}
    text = '\n'.join(render_work_results({}, [task]))
    assert text.index('Error: Model returned') < text.index('Python 3.10.12')
    assert 'finish_reason=length; completion_tokens=2048; max_tokens=2048' in text
    assert 'Worker-reported outcome: partial' in text
    task['result_summary'] = result_view({}, {})
    assert 'reason unavailable' in '\n'.join(render_work_results({}, [task]))


@pytest.mark.asyncio
async def test_readonly_request_gets_consistent_immutable_assignment(db):
    store, work, origin, bid, lid, state = await attached(db)
    await work.configure(bid, lid, experimental_profile())
    req = WorkRequestIn(request_id=uuid4(), source_node_id=state['source_head_node_id'],
        capability='workspace', purpose='experimental_task', objective='Survey the local environment',
        constraints=['read-only inspection only', 'no file creation or modification'])
    pending = await work.request(origin.instance_id, req, character_source=True)
    assert pending['status'] == 'awaiting_approval'
    accepted = await work.decide(origin.instance_id, req.request_id,
        WorkDecision(accept=True, reviewer='operator', reason='Read-only survey'))
    assert accepted['status'] == 'queued'
    run = await db.fetchrow('SELECT assignment FROM aios.workspace_run WHERE run_id=$1', UUID(accepted['run_id']))
    spec = decode(run['assignment'])
    assert requires_read_only(spec['objective'])
    assert spec['expected_outputs'] == spec['required_checks'] == []
    assert 'write the expected JSON report file' not in spec['objective']
