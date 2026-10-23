"""Source passages survive the complete goal-review prompt path."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aios_app.agent.goal_review import source_excerpt, review_revision
from aios_app.agent.transactions import InternalCognitionTransactions
from aios_app.agent.cognitive_operations import CognitiveOperationEngine


def test_review_passages_preserve_outcome_offsets_and_revision_identity():
    node,attempt,timeline=uuid4(),uuid4(),uuid4()
    text='She looked at a magazine.\n'+'Scenery and dialogue.\n'*120+'She attempted rice; the pot overflowed.'
    event=source_excerpt({'node_id':node,'speaker_id':'Renamon','speaker_role':'character',
                          'message_text':text},'Learn to cook')
    assert 'pot overflowed' in event['message_text']
    assert sum(p['end']-p['start'] for p in event['passages'])<=1200
    assert all(text[p['start']:p['end']]==p['text'] for p in event['passages'])
    rev=review_revision('Learn to cook',attempt,timeline,node,[event])
    assert review_revision('Learn to cook',attempt,timeline,node,[event])==rev
    assert review_revision('Learn to cook',uuid4(),timeline,node,[event])!=rev
    assert review_revision('Learn to cook',attempt,uuid4(),node,[event])!=rev
    changed=source_excerpt({'node_id':node,'message_text':text+' A later correction.'},'Learn to cook')
    assert review_revision('Learn to cook',attempt,timeline,node,[changed])!=rev
    assert review_revision('Learn to cook',attempt,timeline,node,[event],'new-evidence')!=rev


@pytest.mark.asyncio
async def test_review_rehydrates_legacy_head_only_excerpts_and_bypasses_micro_hud(monkeypatch):
    node,instance,timeline,operation,txid=[uuid4() for _ in range(5)]
    text='Opening description.\n'+'Irrelevant scenery.\n'*150+'She actually tried cooking. The pot overflowed.'
    row={'node_id':node,'speaker_id':'Renamon','speaker_role':'character','message_text':text}
    captured={}
    async def save(sql,*args):
        if 'INSERT INTO aios.internal_cognition_transaction' in sql:
            captured['prompt']=args[9]
            captured['candidates']=json.loads(args[8])
            return {'transaction_id':txid}
        raise AssertionError(sql)
    db=SimpleNamespace(fetch=AsyncMock(return_value=[row]),execute=AsyncMock(),
        execute_returning_row=save)
    ctx=SimpleNamespace(source_timeline_id=timeline,source_head_node_id=node,state_version=3)
    async def context(*args): return ctx
    async def forbidden(*args,**kwargs): raise AssertionError('micro HUD must not truncate source evidence')
    monkeypatch.setattr('aios_app.hud.context.HUDContextResolver.resolve',context)
    monkeypatch.setattr('aios_app.hud.internal_frame.InternalHUDAssembler.build',forbidden)
    async def enqueue(*args,**kwargs): return uuid4()
    monkeypatch.setattr('aios_app.agent.cognitive_operations.enqueue_job',enqueue)
    await CognitiveOperationEngine(db)._create_decision_transaction({
        'instance_id':instance,'operation_id':operation,'operation_type':'planning.review',
        'source_timeline_id':timeline,'source_node_id':node,'input':{
            'goal':'Renamon wants to learn to cook.','goal_id':str(uuid4()),
            'recent_source_events':[{'node_id':str(node),'message_text':text[:300]}]}})
    assert 'pot overflowed' in captured['prompt']
    assert 'independently verified' in captured['prompt']
    assert 'goal owner' in captured['prompt']
    assert [c['option_index'] for c in captured['candidates']]==list(range(5))
    assert all(c['freshness_policy']=='strict' for c in captured['candidates'])
    assert len(captured['prompt'])<9000


@pytest.mark.asyncio
async def test_review_choice_rejects_changed_source_before_goal_side_effects(monkeypatch):
    instance,node,timeline,opid=[uuid4() for _ in range(4)]
    op={'instance_id':instance,'operation_id':opid,'operation_type':'planning.review',
        'status':'waiting_inference','source_timeline_id':timeline,'source_node_id':node,
        'source_state_version':3}
    db=SimpleNamespace(fetchrow=AsyncMock(return_value=op))
    engine=CognitiveOperationEngine(db)
    stale=AsyncMock()
    monkeypatch.setattr(engine,'_stale',stale)
    async def context(*args):
        return SimpleNamespace(source_timeline_id=timeline,source_head_node_id=uuid4(),state_version=3)
    monkeypatch.setattr('aios_app.hud.context.HUDContextResolver.resolve',context)
    assert not await engine.accept_choice(opid,{'option_index':2})
    stale.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_payload_cannot_cross_into_new_retry_attempt(monkeypatch):
    engine=CognitiveOperationEngine(object())
    stale=AsyncMock()
    monkeypatch.setattr(engine,'_stale',stale)
    await engine._create_decision_transaction({'operation_type':'planning.review',
        'goal_attempt_id':uuid4(),'input':{'attempt_id':str(uuid4())}})
    stale.assert_awaited_once()
    assert 'attempt changed' in stale.await_args.args[1]
