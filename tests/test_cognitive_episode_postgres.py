"""Execute lifecycle SQL against an isolated PostgreSQL engine (PGlite).

Set AIOS_PGLITE_MODULE to the module path after installing @electric-sql/pglite.
This runs no application database or external inference.
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from aios_app.db import Database
from aios_app.agent.episodes import CognitiveEpisodeStore
from aios_app.agent.admission import AutonomyAdmissionService
from aios_app.agent.cognitive_operations import CognitiveOperationEngine
from aios_app.agent.transactions import InternalCognitionTransactions

pytestmark = pytest.mark.skipif(not os.getenv("AIOS_PGLITE_MODULE"), reason="isolated PGlite PostgreSQL engine not configured")
ROOT=Path(__file__).resolve().parents[1]
BOOTSTRAP='''
CREATE SCHEMA aios;
CREATE TABLE aios.character_instance(instance_id uuid PRIMARY KEY);
CREATE TABLE aios.dag_node(node_id uuid PRIMARY KEY,timeline_id uuid,event_id bigint);
CREATE TABLE aios.character_runtime_state(instance_id uuid PRIMARY KEY,source_timeline_id uuid);
CREATE TABLE aios.character_agent_runtime(instance_id uuid PRIMARY KEY,
 pending_cognitive_source_node_id uuid,last_cognitive_source_node_id uuid,
 pending_cognitive_event_count integer DEFAULT 4,cognitive_batch_size integer DEFAULT 4,
 state text DEFAULT 'ready',last_wake_at timestamptz,updated_at timestamptz DEFAULT now());
CREATE TABLE aios.character_wake_event(wake_id uuid DEFAULT gen_random_uuid(),instance_id uuid,
 event_type text,source_type text,source_id text,payload jsonb,priority integer,dedupe_key text,
 available_at timestamptz,status text DEFAULT 'pending',UNIQUE(instance_id,dedupe_key));
CREATE TABLE aios.internal_cognition_transaction(transaction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 instance_id uuid,source_task_id uuid,transaction_kind text DEFAULT 'route',status text DEFAULT 'pending',
 priority integer,source_state_version bigint,source_timeline_id uuid,source_node_id uuid,
 context_fingerprint text,clues jsonb,candidates jsonb,prompt_text text,prompt_hash text,
 opportunity_ids uuid[] DEFAULT '{}',hud_profile_name text DEFAULT 'agent.transaction.route',
 choice_key text,inference_request_id uuid,choice_focus text,result jsonb,rejection_reason text,
 expires_at timestamptz DEFAULT now()+interval '5 minutes',claimed_at timestamptz,completed_at timestamptz,
 consumed_at timestamptz,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now());
CREATE TABLE aios.character_cognitive_opportunity(opportunity_id uuid PRIMARY KEY,instance_id uuid,
 status text DEFAULT 'offered',resolved_at timestamptz,selected_at timestamptz,updated_at timestamptz);
CREATE TABLE aios.character_cognitive_operation(operation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 thread_id uuid,instance_id uuid,opportunity_id uuid,transaction_id uuid,operation_type text,
 input jsonb,result jsonb,source_state_version bigint,source_timeline_id uuid,source_node_id uuid,
 freshness_policy text,source_task_id uuid,status text DEFAULT 'queued',started_at timestamptz,
 completed_at timestamptz,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now());
CREATE TABLE aios.character_cognitive_thread(thread_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 instance_id uuid,subject_key text,status text DEFAULT 'open',pressure float,current_question text,
 source_timeline_id uuid,last_source_node_id uuid,last_state_version bigint,crossing_count integer,
 meta jsonb,subject_id uuid,goal_id uuid,resolved_at timestamptz,last_crossed_at timestamptz,
 updated_at timestamptz DEFAULT now(),UNIQUE(instance_id,subject_key));
CREATE TABLE aios.character_cognitive_thread_crossing(crossing_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 thread_id uuid,opportunity_id uuid,source_node_id uuid,crossing_type text,evidence jsonb,strength float,
 UNIQUE(thread_id,opportunity_id));
CREATE TABLE aios.pipeline_job(job_id uuid DEFAULT gen_random_uuid(),job_type text,payload jsonb,
 status text DEFAULT 'queued',lease_expires_at timestamptz);
'''


def encode(value):
    if isinstance(value, UUID): return str(value)
    if isinstance(value, (list,tuple)): return [encode(v) for v in value]
    return value


class PGConnection:
    def __init__(self, process):
        self.process=process
        self.depth=0

    async def query(self, sql, args=(), exec=False):
        self.process.stdin.write((json.dumps({"sql":sql,"args":encode(args),"exec":exec})+'\n').encode())
        await self.process.stdin.drain()
        response=json.loads(await self.process.stdout.readline())
        if "error" in response: raise RuntimeError(response["error"])
        return response['value']

    async def fetch(self, sql, *args):
        value=await self.query(sql,args)
        uuid_fields={f['name'] for f in value['fields'] if f['dataTypeID']==2950}
        for row in value['rows']:
            for field in uuid_fields:
                if row[field]: row[field]=UUID(row[field])
        return value['rows']

    async def fetchrow(self, sql, *args):
        rows=await self.fetch(sql,*args)
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        row=await self.fetchrow(sql,*args)
        return next(iter(row.values())) if row else None

    async def execute(self, sql, *args):
        return await self.query(sql,args)

    @asynccontextmanager
    async def transaction(self):
        self.depth+=1
        depth=self.depth
        await self.query('BEGIN' if depth==1 else f'SAVEPOINT test_{depth}')
        try:
            yield
        except BaseException:
            await self.query('ROLLBACK' if depth==1 else f'ROLLBACK TO SAVEPOINT test_{depth}')
            raise
        else:
            await self.query('COMMIT' if depth==1 else f'RELEASE SAVEPOINT test_{depth}')
        finally:
            self.depth-=1


class PGPool:
    def __init__(self, connection): self.con=connection
    @asynccontextmanager
    async def acquire(self): yield self.con


@pytest_asyncio.fixture
async def db():
    process=await asyncio.create_subprocess_exec('node',str(ROOT/'tests/cognition_pglite_bridge.mjs'),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
    con=PGConnection(process)
    database=Database('isolated-pglite')
    database.pool=PGPool(con)
    try:
        await con.query(BOOTSTRAP,exec=True)
        await con.query((ROOT/'migrations/current/20261014_01_cognitive_episode_recovery.sql').read_text(),exec=True)
        yield database
    finally:
        process.stdin.close()
        await process.wait()


async def episode_fixture(db):
    instance,source,timeline=uuid4(),uuid4(),uuid4()
    await db.execute('INSERT INTO aios.character_instance VALUES($1)',instance)
    await db.execute('INSERT INTO aios.dag_node VALUES($1,$2,10)',source,timeline)
    await db.execute('INSERT INTO aios.character_runtime_state VALUES($1,$2)',instance,timeline)
    await db.execute('INSERT INTO aios.character_agent_runtime(instance_id,pending_cognitive_source_node_id) VALUES($1,$2)',instance,source)
    store=CognitiveEpisodeStore(db)
    episode=await store.open(instance,source)
    tx=await db.fetchval("INSERT INTO aios.internal_cognition_transaction(instance_id,episode_id,status,reconciled_at,result) VALUES($1,$2,'consumed',now(),'{\"operation\":\"wait\"}') RETURNING transaction_id",instance,episode['episode_id'])
    await store.seal(episode['episode_id'],tx)
    return instance,source,timeline,episode['episode_id'],tx


@pytest.mark.asyncio
async def test_episode_waits_for_both_branches_and_research_can_finish_it(db):
    instance,source,_,episode,_=await episode_fixture(db)
    maintenance=await db.fetchval("INSERT INTO aios.character_cognitive_operation(instance_id,episode_id,episode_role,status,reconciled_at) VALUES($1,$2,'maintenance','succeeded',now()) RETURNING operation_id",instance,episode)
    research=await db.fetchval("INSERT INTO aios.character_cognitive_operation(instance_id,episode_id,episode_role,status,operation_type) VALUES($1,$2,'attention','running','research.advance') RETURNING operation_id",instance,episode)
    store=CognitiveEpisodeStore(db)
    await store.reconcile(episode)
    assert await db.fetchval('SELECT status FROM aios.character_cognitive_episode WHERE episode_id=$1',episode)=='active'
    assert await db.fetchval('SELECT pending_cognitive_event_count FROM aios.character_agent_runtime WHERE instance_id=$1',instance)==4
    await db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded',reconciled_at=now() WHERE operation_id=$1",research)
    await store.reconcile(episode)
    assert await db.fetchval('SELECT status FROM aios.character_cognitive_episode WHERE episode_id=$1',episode)=='succeeded'
    assert await db.fetchval('SELECT pending_cognitive_event_count FROM aios.character_agent_runtime WHERE instance_id=$1',instance)==0


@pytest.mark.asyncio
async def test_wait_without_operations_acknowledges_episode(db):
    instance,source,_,episode,_=await episode_fixture(db)
    await CognitiveEpisodeStore(db).reconcile(episode)
    assert await db.fetchval('SELECT last_cognitive_source_node_id FROM aios.character_agent_runtime WHERE instance_id=$1',instance)==source


@pytest.mark.asyncio
async def test_older_completion_cannot_regress_cursor_or_clear_new_delta(db):
    instance,source,timeline,_,_=await episode_fixture(db)
    newer=uuid4()
    await db.execute('INSERT INTO aios.dag_node VALUES($1,$2,20)',newer,timeline)
    await db.execute('UPDATE aios.character_agent_runtime SET last_cognitive_source_node_id=$2,pending_cognitive_source_node_id=$2 WHERE instance_id=$1',instance,newer)
    await AutonomyAdmissionService(db).mark_episode_succeeded(instance_id=instance,through_node_id=source)
    runtime=await db.fetchrow('SELECT * FROM aios.character_agent_runtime WHERE instance_id=$1',instance)
    assert runtime['last_cognitive_source_node_id']==newer
    assert runtime['pending_cognitive_source_node_id']==newer
    assert runtime['pending_cognitive_event_count']==4


@pytest.mark.asyncio
async def test_failure_rearms_once_without_acknowledging_episode(db):
    instance,source,_,episode,tx=await episode_fixture(db)
    await db.execute("UPDATE aios.internal_cognition_transaction SET status='stale' WHERE transaction_id=$1",tx)
    store=CognitiveEpisodeStore(db)
    await store.reconcile(episode)
    await store.reconcile(episode)
    assert await db.fetchval('SELECT count(*) FROM aios.character_wake_event')==1
    assert await db.fetchval('SELECT pending_cognitive_event_count FROM aios.character_agent_runtime WHERE instance_id=$1',instance)==4


@pytest.mark.asyncio
async def test_terminal_cleanup_rolls_back_and_replays_once(db):
    instance,source,_,episode,_=await episode_fixture(db)
    opid=await db.fetchval("INSERT INTO aios.character_cognitive_operation(instance_id,episode_id,status,operation_type,result) VALUES($1,$2,'succeeded','memory.retrieve','{}') RETURNING operation_id",instance,episode)
    engine=CognitiveOperationEngine(db)
    calls=[]
    async def cleanup(op,result):
        await db.execute('UPDATE aios.character_agent_runtime SET pending_cognitive_event_count=99 WHERE instance_id=$1',instance)
        calls.append(1)
        if len(calls)==1: raise RuntimeError('crashed cleanup')
    engine._finish_side_effects=cleanup
    with pytest.raises(RuntimeError,match='crashed cleanup'):
        await engine.reconcile_terminal(opid)
    assert await db.fetchval('SELECT pending_cognitive_event_count FROM aios.character_agent_runtime WHERE instance_id=$1',instance)==4
    assert await db.fetchval('SELECT reconciled_at FROM aios.character_cognitive_operation WHERE operation_id=$1',opid) is None
    await engine.reconcile_terminal(opid)
    await engine.reconcile_terminal(opid)
    assert len(calls)==2


@pytest.mark.asyncio
async def test_atomic_operation_enqueue_failure_rolls_back_and_retry_deduplicates(db,monkeypatch):
    instance,source,timeline,episode,_=await episode_fixture(db)
    oid=uuid4()
    opportunity={'opportunity_id':oid,'instance_id':instance,'operation_type':'memory.retrieve','source_node_id':source,'source_timeline_id':timeline}
    enqueue=AsyncMock(side_effect=RuntimeError('enqueue failed'))
    monkeypatch.setattr('aios_app.agent.cognitive_operations.enqueue_job',enqueue)
    engine=CognitiveOperationEngine(db)
    with pytest.raises(RuntimeError,match='enqueue failed'):
        await engine.create_from_opportunity(opportunity=opportunity,thread_id=None,episode_id=episode)
    assert await db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==0
    enqueue.side_effect=None
    enqueue.return_value=uuid4()
    first=await engine.create_from_opportunity(opportunity=opportunity,thread_id=None,episode_id=episode)
    second=await engine.create_from_opportunity(opportunity=opportunity,thread_id=None,episode_id=episode)
    assert first==second
    assert await db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==1
    assert enqueue.await_count==2


@pytest.mark.asyncio
async def test_expired_decision_terminates_linked_operation_and_retries_episode(db):
    instance,source,_,episode,_=await episode_fixture(db)
    decision=await db.fetchval("INSERT INTO aios.internal_cognition_transaction(instance_id,episode_id,transaction_kind,status,candidates,expires_at) VALUES($1,$2,'decision','running','[]',now()-interval '1 minute') RETURNING transaction_id",instance,episode)
    opid=await db.fetchval("INSERT INTO aios.character_cognitive_operation(instance_id,episode_id,transaction_id,status,input,operation_type) VALUES($1,$2,$3,'waiting_inference','{}','planning.review') RETURNING operation_id",instance,episode,decision)
    await InternalCognitionTransactions(db)._reject(decision,'stale','deadline')
    op=await db.fetchrow('SELECT status,reconciled_at FROM aios.character_cognitive_operation WHERE operation_id=$1',opid)
    assert op['status']=='stale'
    assert op['reconciled_at'] is not None
    assert await db.fetchval('SELECT status FROM aios.character_cognitive_episode WHERE episode_id=$1',episode)=='failed'
    assert await db.fetchval('SELECT count(*) FROM aios.character_wake_event')==1


@pytest.mark.asyncio
async def test_cursor_rejects_completion_from_previous_timeline(db):
    instance,source,_,_,_=await episode_fixture(db)
    await db.execute('UPDATE aios.character_runtime_state SET source_timeline_id=$2 WHERE instance_id=$1',instance,uuid4())
    await AutonomyAdmissionService(db).mark_episode_succeeded(instance_id=instance,through_node_id=source)
    assert await db.fetchval('SELECT last_cognitive_source_node_id FROM aios.character_agent_runtime WHERE instance_id=$1',instance) is None


@pytest.mark.asyncio
async def test_recovery_repairs_legacy_running_and_failed_decisions(db):
    from aios_app.agent.cognition_recovery import CognitionRecovery
    instance,source,timeline,_,_=await episode_fixture(db)
    decision=await db.fetchval("INSERT INTO aios.internal_cognition_transaction(instance_id,status,candidates,expires_at) VALUES($1,'failed','[]',now()-interval '1 minute') RETURNING transaction_id",instance)
    opid=await db.fetchval("INSERT INTO aios.character_cognitive_operation(instance_id,transaction_id,status,input,operation_type,source_node_id) VALUES($1,$2,'waiting_inference','{}','planning.review',$3) RETURNING operation_id",instance,decision,source)
    abandoned=await db.fetchval("INSERT INTO aios.character_cognitive_operation(instance_id,status,input,operation_type,source_node_id,updated_at) VALUES($1,'running','{}','memory.retrieve',$2,now()-interval '20 minutes') RETURNING operation_id",instance,source)
    await CognitionRecovery(db).recover()
    for oid in [opid,abandoned]:
        assert await db.fetchval('SELECT status FROM aios.character_cognitive_operation WHERE operation_id=$1',oid)=='stale'
    assert await db.fetchval('SELECT count(*) FROM aios.character_wake_event')==2


@pytest.mark.asyncio
async def test_successful_episode_admission_is_not_reopened(db):
    instance,source,_,episode,tx=await episode_fixture(db)
    store=CognitiveEpisodeStore(db)
    await store.reconcile(episode)
    reused=await store.open(instance,source)
    assert reused['episode_id']==episode
    assert reused['route_transaction_id']==tx
    assert await db.fetchval('SELECT count(*) FROM aios.character_cognitive_episode')==1


@pytest.mark.asyncio
async def test_router_admission_rolls_back_entire_fanout_then_reuses_receipt(db,monkeypatch):
    from aios_app.agent.opportunity_router import OpportunityRouter
    from aios_app.agent.opportunities import OpportunityBatch
    from aios_app.hud.internal_frame import InternalHUD
    instance,_,timeline,_,_=await episode_fixture(db)
    source=uuid4()
    await db.execute('INSERT INTO aios.dag_node VALUES($1,$2,20)',source,timeline)
    router=OpportunityRouter(db)
    ids=[uuid4(),uuid4()]
    async def generate(**kwargs):
        rows=[]
        for oid,kind,operation in zip(ids,['memory_recall','goal_review'],['memory.retrieve','planning.review']):
            await db.execute("INSERT INTO aios.character_cognitive_opportunity(opportunity_id,instance_id,status) VALUES($1,$2,'pending')",oid,instance)
            rows.append({'opportunity_id':oid,'instance_id':instance,'opportunity_type':kind,
                'operation_type':operation,'operation_payload':'{}','evidence':'[]','status':'pending',
                'valid_until':True,'priority_score':3,'natural_language':'Consider this',
                'freshness_policy':'contextual','source_node_id':source,'source_timeline_id':timeline})
        return OpportunityBatch(instance,tuple(rows))
    router.opportunities.generate=generate
    router.transactions.huds.build=AsyncMock(return_value=InternalHUD('prompt',1,timeline,source,'fingerprint'))
    fail=True
    async def enqueue(db,*,job_type,payload,priority=100,**kwargs):
        if fail and job_type=='cognitive_operation': raise RuntimeError('reserved enqueue failed')
        return await db.fetchval('INSERT INTO aios.pipeline_job(job_type,payload) VALUES($1,$2::jsonb) RETURNING job_id',job_type,json.dumps(payload))
    monkeypatch.setattr('aios_app.agent.opportunity_router.enqueue_job',enqueue)
    monkeypatch.setattr('aios_app.agent.cognitive_operations.enqueue_job',enqueue)
    with pytest.raises(RuntimeError,match='reserved enqueue failed'):
        await router.admit(instance_id=instance,source_node_id=source)
    assert await db.fetchval('SELECT count(*) FROM aios.internal_cognition_transaction')==1
    assert await db.fetchval('SELECT count(*) FROM aios.character_cognitive_episode')==1
    assert await db.fetchval('SELECT count(*) FROM aios.character_cognitive_opportunity')==0
    assert await db.fetchval('SELECT count(*) FROM aios.pipeline_job')==0
    assert await db.fetchval('SELECT count(*) FROM aios.character_cognitive_thread_crossing')==0
    fail=False
    first=await router.admit(instance_id=instance,source_node_id=source)
    second=await router.admit(instance_id=instance,source_node_id=source)
    assert first==second
    assert await db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==1
    assert await db.fetchval('SELECT count(*) FROM aios.pipeline_job')==2
    assert await db.fetchval('SELECT count(*) FROM aios.internal_cognition_transaction')==2


@pytest.mark.asyncio
async def test_migration_repairs_evidence_and_leaves_failed_legacy_offers_recoverable(db):
    from aios_app.agent.cognition_recovery import CognitionRecovery
    async with db.connection() as con:
        await con.query('DROP SCHEMA aios CASCADE',exec=True)
        await con.query(BOOTSTRAP,exec=True)
        expected=[{'kind':'active_goal'}]
        encoded=await con.fetchval('INSERT INTO aios.character_cognitive_thread_crossing(evidence) VALUES($1::jsonb) RETURNING crossing_id',json.dumps(json.dumps(expected)))
        await con.execute('INSERT INTO aios.character_cognitive_thread_crossing(evidence) VALUES($1::jsonb)',json.dumps('invalid legacy text'))
        instance,oid=uuid4(),uuid4()
        await con.execute('INSERT INTO aios.character_instance VALUES($1)',instance)
        await con.execute("INSERT INTO aios.character_cognitive_opportunity(opportunity_id,instance_id,status) VALUES($1,$2,'offered')",oid,instance)
        failed=await con.fetchval("INSERT INTO aios.internal_cognition_transaction(instance_id,status,candidates,opportunity_ids) VALUES($1,'failed','[]',$2::uuid[]) RETURNING transaction_id",instance,[oid])
        await con.query((ROOT/'migrations/current/20261014_01_cognitive_episode_recovery.sql').read_text(),exec=True)
    evidence=await db.fetchval('SELECT evidence FROM aios.character_cognitive_thread_crossing WHERE crossing_id=$1',encoded)
    assert evidence==expected
    assert await db.fetchval('SELECT reconciled_at FROM aios.internal_cognition_transaction WHERE transaction_id=$1',failed) is None
    await CognitionRecovery(db).recover()
    assert await db.fetchval('SELECT status FROM aios.character_cognitive_opportunity WHERE opportunity_id=$1',oid)=='suppressed'
    assert await db.fetchval('SELECT reconciled_at FROM aios.internal_cognition_transaction WHERE transaction_id=$1',failed) is not None


@pytest.mark.asyncio
async def test_late_inference_cannot_materialize_an_expired_route(db):
    instance,source,_,episode,tx=await episode_fixture(db)
    await db.execute("UPDATE aios.internal_cognition_transaction SET status='pending',reconciled_at=NULL,result=NULL,candidates='[{\"key\":\"E\",\"operation\":\"wait\"}]' WHERE transaction_id=$1",tx)
    service=InternalCognitionTransactions(db)
    service._timely=AsyncMock(return_value=(True,'current'))
    async def infer(request):
        await service._reject(tx,'stale','expired during inference')
        return SimpleNamespace(request_id=uuid4(),response=SimpleNamespace(raw={'choice':'E'}))
    service.broker.infer=infer
    result=await service.run(tx)
    assert result.status=='stale'
    assert await db.fetchval('SELECT result FROM aios.internal_cognition_transaction WHERE transaction_id=$1',tx) is None
    assert await db.fetchval('SELECT pending_cognitive_event_count FROM aios.character_agent_runtime WHERE instance_id=$1',instance)==4
