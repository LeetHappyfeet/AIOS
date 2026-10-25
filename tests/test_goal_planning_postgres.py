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
 started_at timestamptz,completed_at timestamptz,reconciled_at timestamptz,updated_at timestamptz DEFAULT now());
CREATE TABLE aios.character_goal_knowledge_requirement(requirement_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),goal_id uuid,instance_id uuid,
 evidence jsonb DEFAULT '{}'::jsonb,requirement_key text,question text,query_text text,
 coverage_status text,coverage_score float,coverage_source text,retrieval_state text,source_node_id uuid,
 created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now(),UNIQUE(instance_id,goal_id,requirement_key));
CREATE TABLE aios.character_inquiry(instance_id uuid,goal_id uuid,evidence_scope text,status text,
 result jsonb,updated_at timestamptz DEFAULT now());
CREATE TABLE aios.character_goal_research_requirement_link(requirement_id uuid,instance_id uuid,dossier_id uuid,linked_at timestamptz);
CREATE TABLE aios.pipeline_job(job_id uuid DEFAULT gen_random_uuid(),job_type text,payload jsonb,
 status text DEFAULT 'queued',priority integer,run_after timestamptz,resource_class text,
 scheduling_lane text,partition_key text,foreground_instance_id uuid,foreground_node_id uuid,
 foreground_until timestamptz);
'''


@pytest_asyncio.fixture
async def env(monkeypatch):
    process = await asyncio.create_subprocess_exec('node', str(ROOT/'tests/cognition_pglite_bridge.mjs'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    con = PGConnection(process)
    db = Database('isolated'); db.pool = PGPool(con)
    await con.query(BOOTSTRAP, exec=True)
    await con.query((ROOT/'migrations/current/20261014_02_goal_planning.sql').read_text(), exec=True)
    await con.query((ROOT/'migrations/current/20261023_01_goal_review_repair.sql').read_text(), exec=True)
    await con.query((ROOT/'migrations/current/20261025_01_goal_dependency_continuation.sql').read_text(), exec=True)
    await con.query((ROOT/'migrations/current/20261025_02_continuation_lookup_worker.sql').read_text(), exec=True)
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
    from aios_app.entity_memory.retrieval import RDFRecallService
    real_revision = RDFRecallService.discovery_revision
    async def recall_revision(*args): return "relevant-evidence-revision"
    monkeypatch.setattr('aios_app.entity_memory.retrieval.RDFRecallService.discovery_revision', recall_revision)
    async def context(*args): return ctx
    monkeypatch.setattr('aios_app.agent.goal_planning.HUDContextResolver.resolve', context)
    async def epoch(*args): return None
    monkeypatch.setattr('aios_app.topic_atlas.research_dossier.ProgressiveResearchService.corpus_change_epoch', epoch)
    async def enqueue(db, *, job_type, payload, **kwargs):
        return await db.fetchval('INSERT INTO aios.pipeline_job(job_type,payload) VALUES($1,$2::jsonb) RETURNING job_id', job_type, json.dumps(payload))
    monkeypatch.setattr('aios_app.agent.goal_planning.enqueue_job', enqueue)
    async def forbidden_foreground(*args, **kwargs):
        raise AssertionError("small planner must not invoke foreground retrieval")
    monkeypatch.setattr('aios_app.epistemic.cognitive_context.CognitiveContextService.resolve_attention_inputs', forbidden_foreground)
    monkeypatch.setattr('aios_app.epistemic.cognitive_context.CognitiveContextService.resolve_knowledge', forbidden_foreground)
    async def available(*args, **kwargs): return [object()]
    monkeypatch.setattr('aios_app.inference.providers.InferenceProviderStore.routable',available)
    async def infer(*args, **kwargs):
        return SimpleNamespace(request_id=uuid4(), response=SimpleNamespace(raw=proposal()))
    monkeypatch.setattr('aios_app.inference.InferenceBroker.infer', infer)
    try:
        yield SimpleNamespace(db=db,service=GoalPlanningService(db),instance=instance,goal=goal,
                              attempt=attempt,ctx=ctx,monkeypatch=monkeypatch,real_revision=real_revision)
    finally:
        process.stdin.close()
        await process.wait()


async def drain_continuations(e):
    rows = await e.db.fetch("SELECT job_id,payload FROM aios.pipeline_job WHERE job_type='goal_plan_continuation' AND status='queued'")
    for row in rows:
        payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
        await e.db.execute("UPDATE aios.pipeline_job SET status='running' WHERE job_id=$1", row['job_id'])
        await e.service.run_continuation(UUID(payload['plan_id']), request_id=UUID(payload['request_id']))
        await e.db.execute("UPDATE aios.pipeline_job SET status='done' WHERE job_id=$1", row['job_id'])


async def proposed(e, op):
    result = await e.service.propose(op)
    await drain_continuations(e)
    return result


async def continue_background(e):
    await e.service.continue_due()
    await drain_continuations(e)


async def running(e):
    assert await e.service.schedule(e.instance,e.goal)
    return dict(await e.db.fetchrow("UPDATE aios.character_cognitive_operation SET status='running' WHERE status='queued' RETURNING *"))


@pytest.mark.asyncio
async def test_atomic_admission_dedup_and_waiting_plan(env):
    e=env
    op=await running(e)
    assert not await e.service.schedule(e.instance,e.goal)
    result=await proposed(e, op)
    assert result['status']=='waiting_information'
    current=await e.service.current(e.instance,e.goal)
    assert current['proposal']==proposal()
    assert await e.db.fetchval("SELECT count(*) FROM aios.pipeline_job")==3
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded' WHERE operation_type='planning.propose'")
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
    assert (await proposed(e, op))['status']=='waiting_information'
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded'")
    e.ctx.source_head_node_id=uuid4()
    await e.db.execute("INSERT INTO aios.dag_node VALUES($1,$2,2,'Mia','user','There is new information.')",
                       e.ctx.source_head_node_id,e.ctx.source_timeline_id)
    await e.db.execute('INSERT INTO aios.ingest_event VALUES(2,NULL)')
    await e.db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$1', e.ctx.source_head_node_id)
    op=await running(e)
    async def late(*args, **kwargs):
        e.ctx.source_head_node_id=uuid4()
        return SimpleNamespace(request_id=uuid4(),response=SimpleNamespace(raw=proposal()))
    e.monkeypatch.setattr('aios_app.inference.InferenceBroker.infer',late)
    assert (await proposed(e, op))['status']=='stale'
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
    await e.db.execute('''CREATE TABLE IF NOT EXISTS aios.character_inquiry(instance_id uuid,goal_id uuid,
        evidence_scope text,status text,result jsonb,updated_at timestamptz DEFAULT now())''')
    p=proposal(kind='procedure',next_step='research')
    await e.service.continue_once(op,p,snap)
    child=await e.db.fetchrow("SELECT * FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")
    assert child is not None
    payload=json.loads(child['input']) if isinstance(child['input'],str) else child['input']
    assert payload['planning_only'] is True
    assert payload['allow_model'] is False
    snap=await e.service.snapshot(e.instance,e.goal)
    await e.service.continue_once(op,p,snap)
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==2
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded' WHERE operation_type='inquiry.resolve'")
    await e.db.execute('''UPDATE aios.character_cognitive_operation
        SET result='{"status":"unresolved","result":{"reason":"no_access"}}'
        WHERE operation_type='inquiry.resolve' ''')
    await e.service.continue_once(op,p,snap)
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==2


@pytest.mark.asyncio
async def test_malformed_model_response_preserves_previous_accepted_plan(env):
    e=env
    op=await running(e)
    await proposed(e, op)
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded'")
    # A new state revision, even at the same source head, permits a fresh plan.
    e.ctx.state_version=2
    await e.db.execute('UPDATE aios.character_runtime_state SET state_version=2')
    op=await running(e)
    async def malformed(*args,**kwargs):
        return SimpleNamespace(request_id=uuid4(),response=SimpleNamespace(raw={'actions':[]}))
    e.monkeypatch.setattr('aios_app.inference.InferenceBroker.infer',malformed)
    with pytest.raises(ValueError): await proposed(e, op)
    op['status']='failed'
    await e.service.reconcile_terminal(op)
    assert (await e.service.current(e.instance,e.goal))['revision']==1


@pytest.mark.asyncio
async def test_linked_knowledge_requires_accessible_owner_and_live_acquisition(env):
    e=env
    await e.db.pool.con.query('''
      CREATE TABLE aios.proposition(proposition_id uuid PRIMARY KEY,canonical_text text,
        subject_norm text DEFAULT 'Renamon',predicate_norm text DEFAULT 'learned',object_norm text DEFAULT 'cooking');
      CREATE TABLE aios.character_proposition_knowledge(instance_id uuid,proposition_id uuid,
        epistemic_status text,acquisition_mode text,confidence float DEFAULT 1,
        effective_confidence float DEFAULT 1,PRIMARY KEY(instance_id,proposition_id));
      CREATE TABLE aios.knowledge_acquisition_event(instance_id uuid,proposition_id uuid,claim_id uuid);
      CREATE TABLE aios.claim_candidate(claim_id uuid,sentence_id uuid);
      CREATE TABLE aios.extracted_sentence(sentence_id uuid,section_id uuid);
      CREATE TABLE aios.document_section(section_id uuid,node_id uuid);
    ''',exec=True)
    valid,sibling,no_receipt,superseded=[uuid4() for _ in range(4)]
    other=uuid4()
    for key,owner in [(valid,e.instance),(sibling,other),(no_receipt,e.instance),(superseded,e.instance)]:
        await e.db.execute("INSERT INTO aios.proposition(proposition_id,canonical_text) VALUES($1,'A reported fact')",key)
        await e.db.execute("INSERT INTO aios.character_proposition_knowledge(instance_id,proposition_id,epistemic_status,acquisition_mode) VALUES($1,$2,'believed','testimony')",owner,key)
        if key!=no_receipt:
            await e.db.execute('INSERT INTO aios.knowledge_acquisition_event VALUES($1,$2,NULL)',owner,key)
    claim,sentence,section,node=[uuid4() for _ in range(4)]
    await e.db.execute('INSERT INTO aios.ingest_event VALUES(9,now())')
    await e.db.execute('INSERT INTO aios.dag_node(node_id,event_id) VALUES($1,9)',node)
    await e.db.execute('INSERT INTO aios.document_section VALUES($1,$2)',section,node)
    await e.db.execute('INSERT INTO aios.extracted_sentence VALUES($1,$2)',sentence,section)
    await e.db.execute('INSERT INTO aios.claim_candidate VALUES($1,$2)',claim,sentence)
    await e.db.execute('UPDATE aios.knowledge_acquisition_event SET claim_id=$1 WHERE proposition_id=$2',claim,superseded)
    rows,status=await e.service.linked_knowledge(e.ctx,[{'evidence_ids':[str(i) for i in (valid,sibling,no_receipt,superseded)]}])
    assert status=='linked_only' and [r['proposition_id'] for r in rows]==[valid]
    assert rows[0]['epistemic_status']=='believed'
    await e.db.execute('UPDATE aios.character_proposition_knowledge SET effective_confidence=0 WHERE proposition_id=$1',valid)
    rows,status=await e.service.linked_knowledge(e.ctx,[{'evidence_ids':[str(valid)]}])
    assert rows==[] and status=='linked_only'


@pytest.mark.asyncio
async def test_optional_evidence_timeout_does_not_trigger_more_retrieval(env):
    e=env
    async def unavailable(*args,**kwargs): return [],'unavailable'
    e.monkeypatch.setattr(e.service,'linked_knowledge',unavailable)
    async def wants_research(*args,**kwargs):
        return SimpleNamespace(request_id=uuid4(),response=SimpleNamespace(
            raw=proposal(kind='procedure',next_step='research')))
    e.monkeypatch.setattr('aios_app.inference.InferenceBroker.infer',wants_research)
    op=await running(e)
    result=await proposed(e, op)
    assert result['knowledge_context_status']=='unavailable'
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==2


@pytest.mark.asyncio
async def test_environment_inquiry_is_internal_once_per_relevant_evidence_revision(env):
    from aios_app.agent.goal_dependencies import GoalKnowledgeDependencyService
    from aios_app.entity_memory.retrieval import RDFRecallService
    e=env
    op=await running(e)
    snap=await e.service.snapshot(e.instance,e.goal)
    await e.service.continue_once(op,proposal(next_step='internal_lookup'),snap)
    child=await e.db.fetchrow("SELECT * FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")
    payload=json.loads(child['input']) if isinstance(child['input'],str) else child['input']
    assert payload['internal_only'] and not payload['allow_model']
    deps=GoalKnowledgeDependencyService(e.db)
    await deps.record_inquiry(instance_id=e.instance,goal_id=e.goal,
        requirement_id=UUID(payload['requirement_id']),revision=payload['recall_revision'],
        receipt={'inquiry_id':str(uuid4()),'status':'unresolved','result':{'hits':[],'reason':'not_found'}})
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded' WHERE operation_type='inquiry.resolve'")
    snap=await e.service.snapshot(e.instance,e.goal)
    await e.service.continue_once(op,proposal(next_step='internal_lookup'),snap)
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")==1
    async def changed(*args): return 'new-relevant-evidence'
    e.monkeypatch.setattr(RDFRecallService,'discovery_revision',changed)
    snap=await e.service.snapshot(e.instance,e.goal)
    await e.service.continue_once(op,proposal(next_step='internal_lookup'),snap)
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")==2
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='research.advance'")==0


@pytest.mark.asyncio
async def test_runtime_only_changes_do_not_invalidate_advisory_plan(env):
    e=env
    op=await running(e)
    first=await e.service.snapshot(e.instance,e.goal)
    async def state_change(*args,**kwargs):
        e.ctx.state_version+=1
        await e.db.execute('UPDATE aios.character_runtime_state SET state_version=$1',e.ctx.state_version)
        return SimpleNamespace(request_id=uuid4(),response=SimpleNamespace(raw=proposal(next_step='wait')))
    e.monkeypatch.setattr('aios_app.inference.InferenceBroker.infer',state_change)
    assert (await proposed(e, op))['status']=='waiting_information'
    assert first['fingerprint']==(await e.service.snapshot(e.instance,e.goal))['fingerprint']
    assert (await e.service.current(e.instance,e.goal))['context_current']
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded'")
    assert not await e.service.schedule(e.instance,e.goal)


@pytest.mark.asyncio
async def test_unavailable_provider_creates_no_plan_or_operation(env):
    e=env
    async def unavailable(*args,**kwargs): return []
    e.monkeypatch.setattr('aios_app.inference.providers.InferenceProviderStore.routable',unavailable)
    assert not await e.service.schedule(e.instance,e.goal)
    assert await e.service.schedule_due()==0
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_goal_plan')==0
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==0


@pytest.mark.asyncio
async def test_source_change_audit_records_before_inference_and_consumes_no_model(env):
    e=env
    op=await running(e)
    async def forbidden(*args,**kwargs): raise AssertionError('obsolete source must not reach inference')
    e.monkeypatch.setattr('aios_app.inference.InferenceBroker.infer',forbidden)
    e.ctx.source_head_node_id=uuid4()
    result=await proposed(e, op)
    assert result['reason']=='source_node_changed'
    assert result['stage']=='before_inference'
    assert result['inference_request_id'] is None
    assert 'node' in result['changed_inputs']


@pytest.mark.asyncio
async def test_review_revision_index_suppresses_repeated_success_but_allows_retry(env):
    e=env
    payload=json.dumps({'review_revision':'same-attempt-evidence'})
    async def insert(status):
        return await e.db.execute("""INSERT INTO aios.character_cognitive_operation(
            instance_id,operation_type,input,status) VALUES($1,'planning.review',$2::jsonb,$3)""",
            e.instance,payload,status)
    await insert('succeeded')
    with pytest.raises(RuntimeError,match='unique'):
        await insert('queued')
    # A genuinely different evidence/attempt revision can be reviewed.
    await e.db.execute("""INSERT INTO aios.character_cognitive_operation(instance_id,operation_type,input)
        VALUES($1,'planning.review','{"review_revision":"new-attempt-evidence"}')""",e.instance)
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_cognitive_operation')==2


@pytest.mark.asyncio
async def test_deferred_queue_lease_retains_identity_and_checks_owner(env):
    from aios_app.pipeline.jobs import defer_claimed_job
    e=env
    op=await running(e)
    await e.db.execute('''ALTER TABLE aios.pipeline_job
        ADD COLUMN worker_id text, ADD COLUMN attempts int DEFAULT 1,
        ADD COLUMN claimed_at timestamptz, ADD COLUMN heartbeat_at timestamptz,
        ADD COLUMN lease_expires_at timestamptz, ADD COLUMN updated_at timestamptz DEFAULT now()''')
    job=await e.db.fetchval("""UPDATE aios.pipeline_job SET status='running',worker_id='owner',run_after=now()
        RETURNING job_id""")
    assert not await defer_claimed_job(e.db,job,worker_id='wrong-owner',delay_seconds=60)
    assert await defer_claimed_job(e.db,job,worker_id='owner',delay_seconds=60)
    row=await e.db.fetchrow('SELECT status,worker_id,attempts,run_after>now() AS delayed FROM aios.pipeline_job')
    assert row['status']=='queued' and row['worker_id'] is None and row['attempts']==0 and row['delayed']
    assert await e.db.fetchval('SELECT status FROM aios.character_cognitive_operation')=='running'
    assert await e.db.fetchval('SELECT status FROM aios.character_agent_goal')=='active'
    assert await e.db.fetchval('SELECT operation_id FROM aios.character_goal_plan')==op['operation_id']


@pytest.mark.asyncio
async def test_real_revision_sql_failure_persists_dependency_then_recovers_without_model(env, monkeypatch):
    from aios_app.entity_memory.retrieval import RDFRecallService
    e = env
    # Run the production SQL against the fixture which deliberately lacks the
    # projection tables: the old implementation silently lost this dependency.
    original = RDFRecallService.discovery_revision
    monkeypatch.setattr(RDFRecallService, 'discovery_revision', e.real_revision)
    async def cookbook_plan(*args, **kwargs):
        p = proposal()
        p['open_questions'][0]['question'] = 'Is there a cookbook in the cupboard?'
        return SimpleNamespace(request_id=uuid4(), response=SimpleNamespace(raw=p))
    monkeypatch.setattr('aios_app.inference.InferenceBroker.infer', cookbook_plan)
    op = await running(e)
    result = await proposed(e, op)
    assert result['status'] == 'waiting_information'
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_goal_knowledge_requirement') == 1
    plan = await e.service.current(e.instance, e.goal)
    assert plan['continuation']['dependencies'][0]['reason'] == 'evidence_revision_unavailable'
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'") == 0
    monkeypatch.setattr(RDFRecallService, 'discovery_revision', original)
    await e.db.execute("UPDATE aios.character_goal_plan SET continuation_check_at=now()-interval '1 second'")
    from unittest.mock import AsyncMock
    monkeypatch.setattr('aios_app.inference.providers.InferenceProviderStore.routable', AsyncMock(return_value=[]))
    assert await e.service.schedule_due() == 0
    await drain_continuations(e)
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'") == 1
    await continue_background(e)
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='planning.propose'") == 1
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'") == 1


@pytest.mark.asyncio
async def test_independent_procedure_advances_while_environment_revision_is_unavailable(env, monkeypatch):
    from aios_app.entity_memory.retrieval import RDFRecallService
    from unittest.mock import AsyncMock
    e = env
    monkeypatch.setattr(RDFRecallService, 'discovery_revision', AsyncMock(return_value=None))
    op = await running(e)
    snap = await e.service.snapshot(e.instance, e.goal)
    p = proposal()
    p['open_questions'] = [
        {'kind': 'environment', 'question': 'Is there a cookbook in the cupboard?'},
        {'kind': 'procedure', 'question': 'What beginner rice recipes can I practice?'},
        {'kind': 'environment', 'question': 'Is there an internet connected device?'}]
    outcomes = await e.service.continue_once(op, p, snap)
    assert [r['status'] for r in outcomes] == ['unavailable', 'queued', 'unavailable']
    assert len({r['requirement_id'] for r in outcomes}) == 3
    child = await e.db.fetchrow("SELECT input FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")
    payload = json.loads(child['input']) if isinstance(child['input'], str) else child['input']
    assert not payload['internal_only'] and 'rice' in payload['query']
    assert not payload['allow_model']


@pytest.mark.asyncio
async def test_scoped_receipts_do_not_block_another_dependency(env, monkeypatch):
    from unittest.mock import AsyncMock
    from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
    e = env
    op = await running(e)
    snap = await e.service.snapshot(e.instance, e.goal)
    p = proposal(kind='procedure', next_step='research')
    p['open_questions'] = [
        {'kind': 'procedure', 'question': 'How do I measure water for white rice?'},
        {'kind': 'procedure', 'question': 'How do I scramble eggs in a pan?'}]
    outcomes = await e.service.continue_once(op, p, snap)
    await e.db.execute("""UPDATE aios.character_cognitive_operation
        SET status='succeeded',result='{"status":"unresolved","result":{"reason":"no_access"}}'
        WHERE operation_type='inquiry.resolve' AND input->>'requirement_id'=$1""", outcomes[0]['requirement_id'])
    await e.db.execute("""UPDATE aios.character_cognitive_operation
        SET status='succeeded',result='{"status":"partial","result":{"reason":"personal_recall"}}'
        WHERE operation_type='inquiry.resolve' AND input->>'requirement_id'=$1""", outcomes[1]['requirement_id'])
    monkeypatch.setattr(ProgressiveResearchService, 'available', AsyncMock(return_value=True))
    snap = await e.service.snapshot(e.instance, e.goal)
    outcomes = await e.service.continue_once(op, p, snap)
    assert outcomes[0]['status'] == 'denied_access'
    assert outcomes[1]['reason'] == 'research.advance'
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='research.advance'") == 1


@pytest.mark.asyncio
async def test_failed_child_admission_does_not_discard_accepted_plan(env, monkeypatch):
    from unittest.mock import AsyncMock
    e = env
    op = await running(e)
    original_enqueue = __import__('aios_app.agent.goal_planning', fromlist=['enqueue_job']).enqueue_job
    async def fail_child(db, *, job_type, **kwargs):
        return None if job_type == 'cognitive_operation' else await original_enqueue(db, job_type=job_type, **kwargs)
    monkeypatch.setattr('aios_app.agent.goal_planning.enqueue_job', fail_child)
    result = await proposed(e, op)
    assert result['status'] == 'waiting_information'
    plan = await e.service.current(e.instance, e.goal)
    assert plan['proposal'] == proposal()
    assert plan['continuation']['dependencies'][0]['reason'] == 'child_admission_failed'
    assert await e.db.fetchval('SELECT count(*) FROM aios.character_goal_knowledge_requirement') == 1
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'") == 0


@pytest.mark.asyncio
async def test_old_head_cannot_continue_a_plan(env):
    e = env
    op = await running(e)
    await proposed(e, op)
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded'")
    e.ctx.source_head_node_id = uuid4()
    await e.db.execute("UPDATE aios.character_goal_plan SET continuation_check_at=now()-interval '1 second'")
    await continue_background(e)
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'") == 1
    assert await e.db.fetchval("SELECT continuation_check_at FROM aios.character_goal_plan") is None


@pytest.mark.asyncio
async def test_pause_preserves_continuation_for_resume(env, monkeypatch):
    from aios_app.entity_memory.retrieval import RDFRecallService
    from unittest.mock import AsyncMock
    e = env
    original = RDFRecallService.discovery_revision
    monkeypatch.setattr(RDFRecallService, 'discovery_revision', AsyncMock(return_value=None))
    op = await running(e)
    await proposed(e, op)
    await e.db.execute("UPDATE aios.character_agent_runtime SET state='paused'")
    await e.db.execute("UPDATE aios.character_goal_plan SET continuation_check_at=now()-interval '1 second'")
    await continue_background(e)
    assert await e.db.fetchval('SELECT continuation_check_at IS NOT NULL FROM aios.character_goal_plan')
    await e.db.execute("UPDATE aios.character_agent_runtime SET state='ready'")
    await e.db.execute("UPDATE aios.character_goal_plan SET continuation_check_at=now()-interval '1 second'")
    monkeypatch.setattr(RDFRecallService, 'discovery_revision', original)
    await continue_background(e)
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'") == 1


@pytest.mark.asyncio
async def test_transient_inquiry_retry_does_not_repeat_after_recovery(env):
    from aios_app.agent.goal_dependencies import GoalKnowledgeDependencyService
    e = env
    op = await running(e)
    snap = await e.service.snapshot(e.instance, e.goal)
    p = proposal(next_step='internal_lookup')
    await e.service.continue_once(op, p, snap)
    await e.db.execute("""UPDATE aios.character_cognitive_operation SET status='succeeded',
        result='{"status":"unresolved","result":{"reason":"retrieval_unavailable"}}'
        WHERE operation_type='inquiry.resolve'""")
    await e.service.continue_once(op, p, await e.service.snapshot(e.instance, e.goal))
    child = await e.db.fetchrow("SELECT input FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve' AND input->>'recall_revision' LIKE '%retry%'")
    payload = json.loads(child['input']) if isinstance(child['input'], str) else child['input']
    assert payload['recall_revision'] != payload['discovery_revision']
    await GoalKnowledgeDependencyService(e.db).record_inquiry(
        instance_id=e.instance, goal_id=e.goal, requirement_id=UUID(payload['requirement_id']),
        revision=payload['discovery_revision'],
        receipt={'status':'unresolved', 'result':{'hits':[], 'reason':'personal_recall'}})
    await e.db.execute("""UPDATE aios.character_cognitive_operation SET status='succeeded',
        result='{"status":"unresolved","result":{"reason":"personal_recall"}}'
        WHERE operation_type='inquiry.resolve'""")
    outcomes = await e.service.continue_once(op, p, await e.service.snapshot(e.instance, e.goal))
    assert outcomes[0]['status'] == 'awaiting_observation'
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'") == 2


@pytest.mark.asyncio
async def test_supervisor_only_queues_lookup_and_deduplicates(env, monkeypatch):
    from unittest.mock import AsyncMock
    from aios_app.entity_memory.retrieval import RDFRecallService
    from aios_app.pipeline.job_registry import job_spec, scheduling_lane, ResourceClass, SchedulingLane
    from aios_app import runner
    e=env
    revision=AsyncMock(side_effect=AssertionError('supervisor must not discover'))
    monkeypatch.setattr(RDFRecallService,'discovery_revision',revision)
    op=await running(e)
    await e.service.propose(op)
    revision.assert_not_awaited()
    assert await e.db.fetchval("SELECT count(*) FROM aios.pipeline_job WHERE job_type='goal_plan_continuation'")==1
    await e.db.execute("UPDATE aios.character_goal_plan SET continuation_check_at=now()-interval '1 second'")
    assert await e.service.continue_due()==0
    assert job_spec('goal_plan_continuation').resource_class==ResourceClass.LOOKUP
    assert scheduling_lane('goal_plan_continuation')==SchedulingLane.BACKGROUND
    assert runner._worker_limits()[ResourceClass.LOOKUP]==1


@pytest.mark.asyncio
@pytest.mark.parametrize('advance', ['source','timeline','closed_attempt','paused','superseded'])
async def test_source_can_advance_during_discovery_and_stale_work_cannot_admit(env, monkeypatch, advance):
    from aios_app.entity_memory.retrieval import RDFRecallService
    e=env
    entered=asyncio.Event()
    release=asyncio.Event()
    async def slow_revision(*args):
        # Real fixture transaction nesting must be back to zero before IO.
        assert e.db.pool.con.depth==0
        entered.set()
        await release.wait()
        return 'old-evidence'
    monkeypatch.setattr(RDFRecallService,'discovery_revision',slow_revision)
    op=await running(e)
    await e.service.propose(op)
    plan_id=await e.db.fetchval('SELECT plan_id FROM aios.character_goal_plan')
    task=asyncio.create_task(e.service.run_continuation(plan_id))
    await asyncio.wait_for(entered.wait(),5)
    if advance == 'source':
        e.ctx.source_head_node_id=uuid4()
        await asyncio.wait_for(e.db.execute('UPDATE aios.character_runtime_state SET source_head_node_id=$2 WHERE instance_id=$1',e.instance,e.ctx.source_head_node_id),1)
    elif advance == 'timeline':
        e.ctx.source_timeline_id=uuid4()
        await e.db.execute('UPDATE aios.character_runtime_state SET source_timeline_id=$2 WHERE instance_id=$1',e.instance,e.ctx.source_timeline_id)
    elif advance == 'closed_attempt':
        await e.db.execute('UPDATE aios.character_goal_attempt SET closed_at=now()')
    elif advance == 'paused':
        await e.db.execute("UPDATE aios.character_agent_runtime SET state='paused'")
    else:
        await e.db.execute("UPDATE aios.character_goal_plan SET status='superseded'")
    release.set()
    assert await task is False
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")==0
    plan=await e.db.fetchrow('SELECT status,continuation FROM aios.character_goal_plan WHERE plan_id=$1',plan_id)
    continuation=json.loads(plan['continuation']) if isinstance(plan['continuation'],str) else plan['continuation']
    if advance in {'source','timeline'}:
        assert continuation['reason']=='source_context_advanced'
    elif advance=='closed_attempt':
        assert continuation['reason']=='goal_attempt_inactive'
    elif advance=='paused':
        assert continuation['reason']=='runtime_paused'
    else:
        assert plan['status']=='superseded'


@pytest.mark.asyncio
async def test_expired_lease_takeover_fences_late_worker(env, monkeypatch):
    from aios_app.entity_memory.retrieval import RDFRecallService
    from aios_app.pipeline.jobs import JobDeferred
    e=env
    entered=asyncio.Event()
    release=asyncio.Event()
    calls=0
    async def revision(*args):
        nonlocal calls
        calls+=1
        if calls==1:
            entered.set()
            await release.wait()
        return 'same-evidence'
    monkeypatch.setattr(RDFRecallService,'discovery_revision',revision)
    op=await running(e)
    await e.service.propose(op)
    plan_id=await e.db.fetchval('SELECT plan_id FROM aios.character_goal_plan')
    first=asyncio.create_task(e.service.run_continuation(plan_id))
    await asyncio.wait_for(entered.wait(),5)
    with pytest.raises(JobDeferred):
        await e.service.run_continuation(plan_id)
    await e.db.execute("UPDATE aios.character_goal_plan SET continuation_lease_until=now()-interval '1 second'")
    assert await e.service.run_continuation(plan_id)
    release.set()
    assert await first is False
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")==1
    assert await e.db.fetchval('SELECT continuation_token FROM aios.character_goal_plan') is None


@pytest.mark.asyncio
async def test_worker_cancellation_is_recoverable_after_lease_expiry(env, monkeypatch):
    from aios_app.entity_memory.retrieval import RDFRecallService
    from unittest.mock import AsyncMock
    e=env
    entered=asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(RDFRecallService,'discovery_revision',blocked)
    op=await running(e)
    await e.service.propose(op)
    plan_id=await e.db.fetchval('SELECT plan_id FROM aios.character_goal_plan')
    task=asyncio.create_task(e.service.run_continuation(plan_id))
    await asyncio.wait_for(entered.wait(),5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await e.db.fetchval('SELECT continuation_token IS NOT NULL FROM aios.character_goal_plan')
    await e.db.execute("UPDATE aios.character_goal_plan SET continuation_lease_until=now()-interval '1 second'")
    monkeypatch.setattr(RDFRecallService,'discovery_revision',AsyncMock(return_value='recovered'))
    assert await e.service.run_continuation(plan_id)
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")==1


@pytest.mark.asyncio
async def test_same_durable_request_cannot_admit_twice_after_completion(env, monkeypatch):
    from aios_app.entity_memory.retrieval import RDFRecallService
    from unittest.mock import AsyncMock
    e=env
    op=await running(e)
    await e.service.propose(op)
    plan_id=await e.db.fetchval('SELECT plan_id FROM aios.character_goal_plan')
    request=uuid4()
    assert await e.service.run_continuation(plan_id,request_id=request)
    await e.db.execute("UPDATE aios.character_cognitive_operation SET status='succeeded' WHERE operation_type='inquiry.resolve'")
    forbidden=AsyncMock(side_effect=AssertionError('completed request must not rediscover'))
    monkeypatch.setattr(RDFRecallService,'discovery_revision',forbidden)
    assert not await e.service.run_continuation(plan_id,request_id=request)
    forbidden.assert_not_awaited()
    assert await e.db.fetchval("SELECT count(*) FROM aios.character_cognitive_operation WHERE operation_type='inquiry.resolve'")==1


@pytest.mark.asyncio
async def test_queue_constraint_and_resource_class_are_migrated(env):
    e=env
    op=await running(e)
    await e.service.propose(op)
    payload=await e.db.fetchval("SELECT payload FROM aios.pipeline_job WHERE job_type='goal_plan_continuation'")
    async with e.db.atomic():
        with pytest.raises(RuntimeError,match='unique'):
            async with e.db.atomic():
                await e.db.execute("INSERT INTO aios.pipeline_job(job_type,payload,resource_class) VALUES('goal_plan_continuation',$1::jsonb,'LOOKUP')",
                    json.dumps(payload) if isinstance(payload,dict) else payload)
    await e.db.execute("INSERT INTO aios.pipeline_job(job_type,payload,resource_class) VALUES('test','{}','LOOKUP')")
    with pytest.raises(RuntimeError,match='check constraint'):
        async with e.db.atomic():
            await e.db.execute("INSERT INTO aios.pipeline_job(job_type,payload,resource_class) VALUES('test','{}','unknown')")
