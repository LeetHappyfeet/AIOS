import asyncio
import io
import json
import urllib.error
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aios_app.agent.cognitive_operations import CognitiveOperationEngine
from aios_app.agent.goal_planning import GoalPlanningService
from aios_app.inference import InferenceUnavailable
from aios_app.inference.broker import provider_failure,InferenceBroker,InferenceRequest,InferenceProviderExecutionError
from aios_app.pipeline.jobs import JobDeferred


@pytest.mark.asyncio
async def test_provider_unavailability_defers_same_operation_without_goal_failure(monkeypatch):
    op={'instance_id':uuid4(),'operation_id':uuid4(),'status':'running',
        'operation_type':'planning.propose','result':{'deferral_count':2}}
    db=SimpleNamespace(fetchrow=AsyncMock(return_value=op),execute=AsyncMock())
    engine=CognitiveOperationEngine(db)
    async def unavailable(*args): raise InferenceUnavailable('No planning provider')
    monkeypatch.setattr(GoalPlanningService,'propose',unavailable)
    failure=AsyncMock()
    monkeypatch.setattr(engine,'_fail',failure)
    with pytest.raises(JobDeferred) as exc:
        await engine.propose_plan(op['operation_id'])
    assert exc.value.delay_seconds==60
    result=json.loads(db.execute.await_args.args[2])
    assert result['deferral_count']==3
    assert result['kind']=='provider_deferred'
    failure.assert_not_awaited()


def test_staleness_reports_component_and_phase_without_runtime_version_invalidation():
    ctx=SimpleNamespace(source_timeline_id=uuid4(),source_head_node_id=uuid4(),state_version=8)
    op={'operation_id':uuid4()}
    snap={'attempt_id':uuid4(),'context':ctx,'fingerprint':'same','input_context':{'receipts':['new']}}
    plan={'status':'queued','operation_id':op['operation_id'],'attempt_id':snap['attempt_id'],
        'source_timeline_id':ctx.source_timeline_id,'source_node_id':ctx.source_head_node_id,
        'source_state_version':2,'input_fingerprint':'same','input_context':{'receipts':[]}}
    assert GoalPlanningService.fresh(plan,snap,op)
    plan['input_fingerprint']='old'
    reason=GoalPlanningService.stale_reason(plan,snap,op)
    result=GoalPlanningService.stale_result(plan,snap,reason,'after_inference','request-id')
    assert result['reason']=='planning_inputs_changed'
    assert result['changed_inputs']==['receipts']
    assert result['stage']=='after_inference' and result['inference_request_id']=='request-id'
    assert GoalPlanningService.stale_reason(plan,None,op)=='goal_attempt_inactive_or_runtime_paused'


def test_provider_error_retains_bounded_diagnostic_and_redacts_credentials():
    provider=SimpleNamespace(api_key_secret='private-token',api_key_env=None)
    error=urllib.error.HTTPError('https://example.invalid',400,'Bad Request',{},
        io.BytesIO(json.dumps({'error':{'type':'invalid_request','message':'Unsupported JSON mode private-token'}}).encode()))
    detail=provider_failure(error,provider)
    assert detail.status_code==400
    assert 'Unsupported JSON mode' in str(detail)
    assert 'private-token' not in str(detail)
    error=urllib.error.HTTPError('https://example.invalid',429,'Rate limited',{'Retry-After':'120'},io.BytesIO(b'{}'))
    assert provider_failure(error,provider).retry_after==120


@pytest.mark.asyncio
async def test_rate_limit_records_shared_cooldown_instead_of_offline_health():
    provider=SimpleNamespace(provider_id=uuid4())
    db=object()
    broker=InferenceBroker(db)
    broker.providers=SimpleNamespace(routable=AsyncMock(return_value=[provider]),
        record_rate_limit=AsyncMock(),record_health=AsyncMock())
    broker._attempt=AsyncMock(side_effect=InferenceProviderExecutionError('HTTP 429',status_code=429,retry_after=120))
    with pytest.raises(InferenceUnavailable):
        await broker.infer(InferenceRequest(instance_id=uuid4(),worker_class='planning',prompt='test'))
    broker.providers.record_rate_limit.assert_awaited_once_with(provider.provider_id,delay_seconds=120,error='HTTP 429')
    broker.providers.record_health.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_returns_deferred_lease_without_marking_job_done_or_failed(monkeypatch):
    from aios_app import runner
    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(execute=AsyncMock())
    db=SimpleNamespace(connection=connection,execute=AsyncMock())
    async def handler(*args): raise JobDeferred('provider cooling down',delay_seconds=60)
    async def heartbeat(*args,**kwargs): await asyncio.Event().wait()
    monkeypatch.setitem(runner.JOB_HANDLERS,'goal_planning_inference',handler)
    monkeypatch.setattr(runner,'job_spec',lambda *args:SimpleNamespace(
        isolate_blocking=False,requires_rdf_slot=False,resource_class=SimpleNamespace(value='planning')))
    monkeypatch.setattr(runner,'admit_job',AsyncMock(return_value=True))
    monkeypatch.setattr(runner,'_resolve_partition_key',AsyncMock(return_value='instance:test'))
    monkeypatch.setattr(runner,'_heartbeat_loop',heartbeat)
    defer,done,failed=AsyncMock(),AsyncMock(),AsyncMock()
    monkeypatch.setattr(runner,'defer_claimed_job',defer)
    monkeypatch.setattr(runner,'mark_done',done)
    monkeypatch.setattr(runner,'mark_failed',failed)
    job={'job_id':uuid4(),'job_type':'goal_planning_inference','payload':{}}
    await runner._execute_claimed_job(db,job=job,worker_id='owner',
        rdf_gate=asyncio.Semaphore(1),resource_governor=SimpleNamespace(release=AsyncMock()))
    defer.assert_awaited_once_with(db,job['job_id'],worker_id='owner',delay_seconds=60)
    done.assert_not_awaited()
    failed.assert_not_awaited()


def test_planning_requirements_ignore_retry_metadata_and_evidence_order():
    from aios_app.agent.goal_planning import planning_requirements
    requirement = {'requirement_id':'one','question':'Where is the sink?',
                   'coverage_status':'missing','evidence_ids':['b','a'],
                   'rdf_refs':[], 'recall_receipt':{'inquiry_id':'old','revision':'old'},
                   'retrieval_state':'unassessed'}
    retried = {**requirement,'evidence_ids':['a','b'],
               'recall_receipt':{'inquiry_id':'new','revision':'new'},
               'retrieval_state':'internal_recall'}
    assert planning_requirements([requirement]) == planning_requirements([retried])
    assert planning_requirements([requirement]) != planning_requirements([{**retried,'coverage_status':'sufficient'}])


def test_plan_text_limits_and_diagnostics():
    from aios_app.agent.goal_planning import validate_plan, PlanTextError, build_plan_prompt
    from test_goal_planning import proposal
    raw = {**proposal(), 'strategy':'a'*600, 'steps':['b'*400]}
    assert validate_plan(raw, {'goal'}) == raw
    for field, change, expected, maximum in (
        ('strategy', {'strategy':'a'*601}, 601, 600),
        ('steps[0]', {'steps':['b'*401]}, 401, 400),
        ('open_questions[0].question', {'open_questions':[{'kind':'environment','question':'c'*181}]}, 181, 180),
    ):
        with pytest.raises(PlanTextError) as error:
            validate_plan({**raw, **change}, {'goal'})
        assert error.value.details == {'field':field,'reason':'too_long','actual':expected,'maximum':maximum}
    for value, reason in (('', 'empty'), (None, 'expected_string')):
        with pytest.raises(PlanTextError) as error:
            validate_plan({**raw, 'strategy':value}, {'goal'})
        assert error.value.details['reason'] == reason
    prompt, _ = build_plan_prompt('Learn to cook', [], [], [], {}, [])
    assert 'strategy 1-600' in prompt and 'each step 1-400' in prompt and 'each question 1-180' in prompt
    # The two observed provider outputs fit without another paid call.
    assert validate_plan({**proposal(), 'strategy':'s'*282, 'steps':['t'*211]}, {'goal'})


@pytest.mark.asyncio
async def test_exhausted_text_correction_is_recorded_without_worker_exception(monkeypatch):
    from aios_app.agent.goal_planning import PlanTextError
    op={'instance_id':uuid4(),'operation_id':uuid4(),'status':'running',
        'operation_type':'planning.propose'}
    db=SimpleNamespace(fetchrow=AsyncMock(return_value=op),
                       execute_returning_row=AsyncMock(return_value={'operation_id':op['operation_id']}))
    engine=CognitiveOperationEngine(db)
    error=PlanTextError('steps[0]', 'too_long', actual=401, maximum=400)
    error.request_ids=['original', 'correction']
    monkeypatch.setattr(GoalPlanningService, 'propose', AsyncMock(side_effect=error))
    reconcile=AsyncMock()
    finish=AsyncMock()
    monkeypatch.setattr(engine, 'reconcile_terminal', reconcile)
    monkeypatch.setattr(engine, '_finish', finish)
    await engine.propose_plan(op['operation_id'])
    saved=json.loads(db.execute_returning_row.await_args.args[2])
    assert saved['validation']['field'] == 'steps[0]'
    assert saved['inference_request_ids'] == ['original','correction']
    assert saved['reason']=='plan_text_validation_failed'
    assert "status='failed'" in db.execute_returning_row.await_args.args[0]
    reconcile.assert_awaited_once_with(op['operation_id'])
    finish.assert_not_awaited()
