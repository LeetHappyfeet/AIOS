"""Goal plans against isolated PostgreSQL; no live application DB or model."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from aios_app.agent.goal_planning import GoalPlanningService
from aios_app.db import Database
from test_cognitive_episode_postgres import PGConnection, PGPool
from test_goal_planning import proposal

pytestmark = pytest.mark.skipif(not os.getenv("AIOS_PGLITE_MODULE"), reason="PGlite not configured")
ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = '''
CREATE SCHEMA aios;
CREATE TABLE aios.character_instance(instance_id uuid PRIMARY KEY);
CREATE TABLE aios.dag_node(node_id uuid PRIMARY KEY,timeline_id uuid,event_id bigint,
 speaker_id text,speaker_role text,message_text text);
CREATE TABLE aios.ingest_event(event_id bigint PRIMARY KEY,superseded_at timestamptz);
CREATE TABLE aios.character_agent_goal(goal_id uuid PRIMARY KEY,instance_id uuid,goal_text text,
 status text DEFAULT 'active',priority integer DEFAULT 100,created_at timestamptz DEFAULT now());
CREATE TABLE aios.character_goal_attempt(attempt_id uuid PRIMARY KEY,goal_id uuid,instance_id uuid,
 closed_at timestamptz,UNIQUE(attempt_id,goal_id,instance_id));
CREATE TABLE aios.character_runtime_state(instance_id uuid PRIMARY KEY,source_timeline_id uuid,
 source_head_node_id uuid,state_version bigint DEFAULT 1);
CREATE TABLE aios.character_agent_runtime(instance_id uuid PRIMARY KEY,state text DEFAULT 'ready');
CREATE TABLE aios.character_hud_readiness(instance_id uuid PRIMARY KEY,status text,
 dirty_since timestamptz,updated_at timestamptz);
CREATE TABLE aios.character_cognitive_operation(operation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 instance_id uuid,operation_type text,input jsonb,status text DEFAULT 'queued',result jsonb,
 source_state_version bigint,source_timeline_id uuid,source_node_id uuid,freshness_policy text,
 completed_at timestamptz);
CREATE TABLE aios.character_goal_knowledge_requirement(requirement_id uuid,goal_id uuid,instance_id uuid,
 requirement_key text,question text,query_text text,coverage_status text,coverage_score float,retrieval_state text,created_at timestamptz);
CREATE TABLE aios.character_goal_research_requirement_link(requirement_id uuid,instance_id uuid,dossier_id uuid,linked_at timestamptz);
CREATE TABLE aios.pipeline_job(job_id uuid DEFAULT gen_random_uuid(),job_type text,payload jsonb);
'''


@pytest_asyncio.fixture
async def env(monkeypatch):
    process = await asyncio.create_subprocess_exec('node', str(ROOT/'tests/cognition_pglite_bridge.mjs'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    con = PGConnection(process)
    db = Database('isolated'); db.pool = PGPool(con)
    await con.query(BOOTSTRAP, exec=True)
    await con.query((ROOT/'migrations/current/20261014_02_goal_planning.sql').read_text(), exec=True)
    instance, goal, attempt, timeline, node = [uuid4() for _ in range(5)]
    await db.execute('INSERT INTO aios.character_instance VALUES($1)', instance)
    await db.execute("INSERT INTO aios.character_agent_goal(goal_id,instance_id,goal_text) VALUES($1,$2,'Learn to cook')", goal, instance)
    await db.execute('INSERT INTO aios.character_goal_attempt(attempt_id,goal_id,instance_id) VALUES($1,$2,$3)', attempt, goal, instance)
    await db.execute('INSERT INTO aios.character_runtime_state(instance_id,source_timeline_id,source_head_node_id) VALUES($1,$2,$3)', instance, timeline, node)
    await db.execute('INSERT INTO aios.character_agent_runtime(instance_id) VALUES($1)', instance)
    await db.execute("INSERT INTO aios.character_hud_readiness(instance_id,status) VALUES($1,'ready')", instance)
    await db.execute("INSERT INTO aios.ingest_event VALUES(1,NULL)")
    await db.execute("INSERT INTO aios.dag_node VALUES($1,$2,1,'Mia','user','You need to learn independently.')", node, timeline)
    ctx = SimpleNamespace(source_timeline_id=timeline, source_head_node_id=node, state_version=1,
                          instance_id=instance, character_id='Renamon')
    async def context(*args): return ctx
    monkeypatch.setattr('aios_app.agent.goal_planning.HUDContextResolver.resolve', context)
    async def epoch(*args): return None
    monkeypatch.setattr('aios_app.topic_atlas.research_dossier.ProgressiveResearchService.corpus_change_epoch', epoch)
    async def enqueue(db, *, job_type, payload, **kwargs):
        return await db.fetchval('INSERT INTO aios.pipeline_job(job_type,payload) VALUES($1,$2::jsonb) RETURNING job_id', job_type, json.dumps(payload))
    monkeypatch.setattr('aios_app.agent.goal_planning.enqueue_job', enqueue)
    async def attention(*args, **kwargs): return None
    async def knowledge(*args, **kwargs): return SimpleNamespace(knowledge=[])
    monkeypatch.setattr('aios_app.epistemic.cognitive_context.CognitiveContextService.resolve_attention_inputs', attention)
    monkeypatch.setattr('aios_app.epistemic.cognitive_context.CognitiveContextService.resolve_knowledge', knowledge)
    async def infer(*args, **kwargs):
        return SimpleNamespace(request_id=uuid4(), response=SimpleNamespace(raw=proposal()))
    monkeypatch.setattr('aios_app.inference.InferenceBroker.infer', infer)
    try:
        yield SimpleNamespace(db=db,service=GoalPlanningService(db),instance=instance,goal=goal,
                              attempt=attempt,ctx=ctx,monkeypatch=monkeypatch)
    finally:
        process.stdin.close()
        await process.wait()


async def running(e):
    assert await e.service.schedule(e.instance,e.goal)
    return dict(await e.db.fetchrow("UPDATE aios.character_cognitive_operation SET status='running' WHERE status='queued' RETURNING *"))


@pytest.mark.asyncio
async def test_atomic_admission_dedup_and_waiting_plan(env):
    e=env
    op=await running(e)
    assert not await e.service.schedule(e.instance,e.goal)
    result=await e.service.propose(op)
    assert result['status']=='waiting_information'
    current=await e.service.current(e.instance,e.goal)
    assert current['proposal']==proposal()
    assert await e.db.fetchval("SELECT count(*) FROM aios.pipeline_job")==1
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded'")
    assert not await e.service.schedule(e.instance,e.goal)
    assert await e.db.fetchval("SELECT status FROM aios.character_hud_readiness")=='dirty'


@pytest.mark.asyncio
async def test_ownership_and_closed_attempt_cannot_get_a_plan(env):
    e=env
    assert not await e.service.schedule(uuid4(),e.goal)
    await e.db.execute('UPDATE aios.character_goal_attempt SET closed_at=now()')
    assert not await e.service.schedule(e.instance,e.goal)


@pytest.mark.asyncio
async def test_late_inference_is_rejected_and_previous_plan_survives_failure(env):
    e=env
    op=await running(e)
    assert (await e.service.propose(op))['status']=='waiting_information'
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded'")
    e.ctx.source_head_node_id=uuid4()
    await e.db.execute("INSERT INTO aios.dag_node VALUES($1,$2,2,'Mia','user','There is new information.')",
                       e.ctx.source_head_node_id,e.ctx.source_timeline_id)
    await e.db.execute('INSERT INTO aios.ingest_event VALUES(2,NULL)')
    await e.db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$1', e.ctx.source_head_node_id)
    op=await running(e)
    async def late(*args, **kwargs):
        e.ctx.state_version+=1
        return SimpleNamespace(request_id=uuid4(),response=SimpleNamespace(raw=proposal()))
    e.monkeypatch.setattr('aios_app.inference.InferenceBroker.infer',late)
    assert (await e.service.propose(op))['status']=='stale'
    op['status']='stale'
    await e.service.reconcile_terminal(op)
    current=await e.service.current(e.instance,e.goal)
    assert current['revision']==1
    assert not current['context_current']


@pytest.mark.asyncio
async def test_goal_closure_hides_plan_and_abandoned_operation_releases_slot(env):
    e=env
    op=await running(e)
    op['status']='failed'
    await e.service.reconcile_terminal(op)
    assert await e.db.fetchval('SELECT status FROM aios.character_goal_plan')=='failed'
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='failed'")
    # Failed same-input work is not regenerated on every scheduler tick.
    assert not await e.service.schedule(e.instance,e.goal)
    await e.db.execute("UPDATE aios.character_agent_goal SET status='completed'")
    assert await e.service.current(e.instance,e.goal) is None


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_admission(env):
    e=env
    async def refused(*args, **kwargs): return None
    e.monkeypatch.setattr('aios_app.agent.goal_planning.enqueue_job', refused)
    with pytest.raises(RuntimeError, match='enqueue'):
        await e.service.schedule(e.instance,e.goal)
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_goal_plan')==0
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==0


@pytest.mark.asyncio
async def test_paused_agent_is_not_scheduled_and_primary_goal_bypasses_chat_batch(env):
    e=env
    await e.db.execute("UPDATE aios.character_agent_runtime SET state='paused'")
    assert await e.service.schedule_due()==0
    await e.db.execute("UPDATE aios.character_agent_runtime SET state='ready'")
    assert await e.service.schedule_due()==1
    assert await e.service.schedule_due()==0


@pytest.mark.asyncio
async def test_procedure_dispatches_only_one_host_owned_inquiry_and_no_access_stops(env):
    e=env
    from unittest.mock import AsyncMock
    from aios_app.agent.goal_dependencies import GoalKnowledgeDependencyService
    from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
    op=await running(e)
    snap=await e.service.snapshot(e.instance,e.goal)
    await e.db.execute('''CREATE TABLE aios.character_inquiry(instance_id uuid,goal_id uuid,
        evidence_scope text,status text,result jsonb,updated_at timestamptz DEFAULT now())''')
    requirement=AsyncMock(return_value={'requirement_id':uuid4()})
    e.monkeypatch.setattr(GoalKnowledgeDependencyService,'reconcile',requirement)
    p=proposal(kind='procedure',next_step='research')
    await e.service.continue_once(op,p,snap)
    child=await e.db.fetchrow("SELECT * FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")
    assert child is not None
    payload=json.loads(child['input']) if isinstance(child['input'],str) else child['input']
    assert payload['planning_only'] is True
    assert payload['allow_model'] is False
    await e.service.continue_once(op,p,snap)
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==2
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded' WHERE operation_type='inquiry.resolve'")
    await e.db.execute('''INSERT INTO aios.character_inquiry(instance_id,goal_id,evidence_scope,status,result)
        VALUES($1,$2,'character_accessible','unresolved','{"reason":"no_access"}')''',e.instance,e.goal)
    await e.service.continue_once(op,p,snap)
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==2


@pytest.mark.asyncio
async def test_malformed_model_response_preserves_previous_accepted_plan(env):
    e=env
    op=await running(e)
    await e.service.propose(op)
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded'")
    # A new state revision, even at the same source head, permits a fresh plan.
    e.ctx.state_version=2
    await e.db.execute('UPDATE aios.character_runtime_state SET state_version=2')
    op=await running(e)
    async def malformed(*args,**kwargs):
        return SimpleNamespace(request_id=uuid4(),response=SimpleNamespace(raw={'actions':[]}))
    e.monkeypatch.setattr('aios_app.inference.InferenceBroker.infer',malformed)
    with pytest.raises(ValueError): await e.service.propose(op)
    op['status']='failed'
    await e.service.reconcile_terminal(op)
    assert (await e.service.current(e.instance,e.goal))['revision']==1
