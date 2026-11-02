from unittest.mock import AsyncMock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aios_app.agent.goal_activity import continuation_retry, observe_interaction
from aios_app.agent.goal_planning import GoalPlanningService
from aios_app.pipeline.jobs import JobDeferred


def test_unchanged_evidence_backs_off_and_new_evidence_resets():
    args = dict(source='node', requirements=[{'coverage_status': 'missing'}],
                discoveries={'need': ('revision', {'elapsed_ms': 12})})
    signature, count, delay = continuation_retry(None, 0, **args)
    assert (count, delay) == (0, 60)
    for expected in (120, 240, 480, 900, 900):
        args['discoveries']['need'] = ('revision', {'elapsed_ms': expected})
        signature, count, delay = continuation_retry(signature, count, **args)
        assert delay == expected
    args['discoveries']['need'] = ('changed-evidence', {})
    assert continuation_retry(signature, count, **args)[1:] == (0, 60)
    args['source'] = 'new-node'
    assert continuation_retry(signature, count, **args)[1:] == (0, 60)


@pytest.mark.asyncio
async def test_dormant_queued_continuation_performs_no_expensive_work():
    db = SimpleNamespace(fetchval=AsyncMock(side_effect=[uuid4(), False]),
                         atomic=AsyncMock(side_effect=AssertionError('must not lease')))
    assert not await GoalPlanningService(db).run_continuation(uuid4())
    db.atomic.assert_not_called()


@pytest.mark.asyncio
async def test_dormant_queued_plan_defers_before_snapshot_or_inference():
    db = SimpleNamespace(fetchval=AsyncMock(return_value=False),
                         fetchrow=AsyncMock(side_effect=AssertionError('must not snapshot')))
    with pytest.raises(JobDeferred, match='inactive or paused'):
        await GoalPlanningService(db).propose({'instance_id': uuid4()})
    db.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_new_interaction_resumes_existing_needs_without_changing_authority():
    db = SimpleNamespace(execute=AsyncMock())
    instance = uuid4()
    await observe_interaction(db, instance)
    assert len(db.execute.await_args_list) == 2
    assert all(call.args[1] == instance for call in db.execute.await_args_list)
    assert 'continuation_check_at=now()' in db.execute.await_args_list[1].args[0]
    assert 'background_enabled' not in db.execute.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_parked_inference_does_not_fail_operation(monkeypatch):
    from aios_app.agent.cognitive_operations import CognitiveOperationEngine
    op = {'instance_id': uuid4(), 'operation_id': uuid4(), 'status': 'running',
          'operation_type': 'planning.propose'}
    db = SimpleNamespace(fetchrow=AsyncMock(return_value=op), fetchval=AsyncMock(return_value=False))
    engine = CognitiveOperationEngine(db)
    monkeypatch.setattr(engine, '_fail', AsyncMock())
    with pytest.raises(JobDeferred):
        await engine.propose_plan(op['operation_id'])
    engine._fail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('speaker,kind,expected', [('character','chat_message',True),
    ('user','chat_message',True),('tool','tool_result',False)])
async def test_ingestion_refreshes_only_new_conversation_activity(monkeypatch, speaker, kind, expected):
    from test_ingest_replay_pipeline import FakeDB, request, install_structural_mocks
    from aios_app import ingest_api
    from aios_app.agent import goal_activity, admission
    instance = uuid4()
    db = FakeDB({'event_id': 81, 'inserted': True, 'was_superseded': False},
                [None, {'source_head_node_id': uuid4()}])
    install_structural_mocks(monkeypatch, instance_ids=[instance])
    for name in ('ensure_participant', 'bind_available_instance', 'record_message_participation'):
        monkeypatch.setattr(ingest_api, name, AsyncMock(return_value=uuid4()))
    observe = AsyncMock()
    monkeypatch.setattr(goal_activity, 'observe_interaction', observe)
    monkeypatch.setattr(admission.AutonomyAdmissionService, 'observe_host_experience', AsyncMock())
    req = request(message_id=50)
    req.speaker_type, req.kind = speaker, kind
    req.speaker_id = 'Mia' if speaker == 'user' else ('workspace' if speaker == 'tool' else 'Alex')
    await ingest_api.ingest_message(db, req)
    assert observe.await_count == int(expected)
    if expected:
        observe.assert_awaited_once_with(db, instance)
