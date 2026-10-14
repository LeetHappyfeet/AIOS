import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aios_app.db import Database
from aios_app.agent.threads import CognitiveThreadService


class Connection:
    def __init__(self, name): self.name=name; self.depth=0
    @asynccontextmanager
    async def transaction(self):
        self.depth+=1
        try: yield
        finally: self.depth-=1
    async def fetchval(self, *args): return self.name


class Pool:
    def __init__(self): self.connections=[]
    @asynccontextmanager
    async def acquire(self):
        con=Connection(len(self.connections))
        self.connections.append(con)
        yield con


@pytest.mark.asyncio
async def test_services_share_owner_connection_but_child_tasks_do_not():
    db=Database('test')
    db.pool=Pool()
    async with db.atomic():
        owner=await db.fetchval('SELECT 1')
        async with db.atomic():
            assert await db.fetchval('SELECT 1')==owner
        child=await asyncio.create_task(db.fetchval('SELECT 1'))
        assert child != owner
        assert await db.fetchval('SELECT 1')==owner
    assert await db.fetchval('SELECT 1') != owner
    assert len(db.pool.connections)==3


@pytest.mark.asyncio
async def test_atomic_binding_restored_after_exception():
    db=Database('test')
    db.pool=Pool()
    with pytest.raises(RuntimeError):
        async with db.atomic():
            await db.fetchval('SELECT 1')
            raise RuntimeError('rollback')
    assert db._transaction_connection.get() is None
    assert await db.fetchval('SELECT 1')==1


@pytest.mark.asyncio
async def test_crossing_evidence_json_string_is_stored_as_array():
    db=SimpleNamespace(execute_returning_row=AsyncMock(side_effect=[
        {'thread_id':uuid4()},{'crossing_id':uuid4()}]),execute=AsyncMock())
    evidence=[{'kind':'active_goal','goal_id':str(uuid4())}]
    await CognitiveThreadService(db).cross({'instance_id':uuid4(),'opportunity_id':uuid4(),
        'evidence':json.dumps(evidence)})
    persisted=db.execute_returning_row.await_args_list[1].args[5]
    assert json.loads(persisted)==evidence


@pytest.mark.asyncio
async def test_rejected_task_attention_notifies_parent_once(monkeypatch):
    from aios_app.agent.transactions import InternalCognitionTransactions
    from aios_app.agent.lifecycle import CharacterAgencyStore
    from aios_app.agent.runtime import AgentRuntimeStore
    instance,taskid,parent,txid=uuid4(),uuid4(),uuid4(),uuid4()
    tx={'status':'failed','reconciled_at':None,'episode_id':None,'source_task_id':taskid,
        'opportunity_ids':[],'candidates':'[]'}
    @asynccontextmanager
    async def atomic(): yield
    async def execute(sql,*args):
        if 'SET reconciled_at=now()' in sql: tx['reconciled_at']=True
    db=SimpleNamespace(atomic=atomic,fetchrow=AsyncMock(return_value=tx),
                       fetch=AsyncMock(return_value=[]),execute=execute)
    task=SimpleNamespace(task_id=taskid,status='waiting',instance_id=instance,parent_task_id=parent,priority=100)
    monkeypatch.setattr(CharacterAgencyStore,'get_task',AsyncMock(return_value=task))
    transition=AsyncMock()
    monkeypatch.setattr(CharacterAgencyStore,'transition_task',transition)
    monkeypatch.setattr(AgentRuntimeStore,'finish_semantic_turn',AsyncMock())
    wake=AsyncMock()
    monkeypatch.setattr(AgentRuntimeStore,'wake',wake)
    service=InternalCognitionTransactions(db)
    await service.reconcile_terminal(txid)
    await service.reconcile_terminal(txid)
    transition.assert_awaited_once()
    wake.assert_awaited_once()
    assert wake.await_args.kwargs['payload']['parent_task_id']==str(parent)


@pytest.mark.asyncio
async def test_one_failed_outbox_repair_does_not_block_next(monkeypatch):
    from aios_app.agent.cognition_recovery import CognitionRecovery
    from aios_app.agent.episodes import CognitiveEpisodeStore
    @asynccontextmanager
    async def atomic(): yield
    db=SimpleNamespace(atomic=atomic,fetchval=AsyncMock(return_value=None),
        fetch=AsyncMock(side_effect=[[],[],[],[],[
            {'operation_id':uuid4(),'instance_id':uuid4()},
            {'operation_id':uuid4(),'instance_id':uuid4()},
        ]]))
    enqueue=AsyncMock(side_effect=[RuntimeError('failed outbox'),uuid4()])
    monkeypatch.setattr('aios_app.pipeline.jobs.enqueue_job',enqueue)
    episodes=AsyncMock()
    monkeypatch.setattr(CognitiveEpisodeStore,'reconcile_pending',episodes)
    await CognitionRecovery(db).recover()
    assert enqueue.await_count==2
    episodes.assert_awaited_once()
