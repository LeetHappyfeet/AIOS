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
