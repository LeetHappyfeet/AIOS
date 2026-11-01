"""Real PostgreSQL admission, wake-up, fairness and lease selection checks."""
import os
import json
from uuid import uuid4

import pytest
from test_goal_planning_postgres import env, running
from aios_app.agent.goal_activity import observe_interaction, set_background, grant_scheduled_window
from aios_app.pipeline.jobs import fetch_next_job

pytestmark = pytest.mark.skipif(not os.getenv('AIOS_PGLITE_MODULE'), reason='PGlite not configured')


@pytest.mark.asyncio
async def test_dormancy_resume_pause_and_explicit_background(env):
    e = env
    op = await running(e)
    await e.service.propose(op)
    await e.db.execute("UPDATE aios.instance_goal_activity SET last_interaction_at=now()-interval '2 hours'")
    assert not await e.service.run_continuation(await e.db.fetchval('SELECT plan_id FROM aios.character_goal_plan'))
    assert await e.service.continue_due() == 0
    assert await e.service.schedule_due() == 0
    assert await e.db.fetchval('SELECT activity_band FROM aios.instance_goal_scheduling') == 3
    await set_background(e.db, e.instance, True)
    assert await e.db.fetchval('SELECT eligible FROM aios.instance_goal_scheduling')
    await set_background(e.db, e.instance, False)
    await grant_scheduled_window(e.db, e.instance)
    assert await e.db.fetchval('SELECT eligible FROM aios.instance_goal_scheduling')
    assert await e.db.fetchval('SELECT activity_band FROM aios.instance_goal_scheduling') == 2
    await e.db.execute("UPDATE aios.instance_goal_activity SET explicit_work_until=now()-interval '1 second'")
    assert not await e.db.fetchval('SELECT eligible FROM aios.instance_goal_scheduling')
    await observe_interaction(e.db, e.instance)
    assert await e.db.fetchval('SELECT activity_band FROM aios.instance_goal_scheduling') == 0
    assert await e.db.fetchval('SELECT continuation_check_at<=now() FROM aios.character_goal_plan')
    await e.db.execute("UPDATE aios.character_agent_runtime SET state='paused'")
    assert not await e.db.fetchval('SELECT eligible FROM aios.instance_goal_scheduling')


async def add_instance(e, *, age='0 seconds', background=False):
    instance = uuid4()
    await e.db.execute('INSERT INTO aios.character_instance VALUES($1)', instance)
    await e.db.execute('INSERT INTO aios.character_agent_runtime(instance_id) VALUES($1)', instance)
    await observe_interaction(e.db, instance)
    await e.db.execute("UPDATE aios.instance_goal_activity SET last_interaction_at=now()-$2::interval,background_enabled=$3 WHERE instance_id=$1",
                       instance, age, background)
    return instance


@pytest.mark.asyncio
async def test_actual_queue_prioritizes_active_rotates_and_parks_dormant(env):
    e = env
    await e.db.pool.con.query('''
    ALTER TABLE aios.pipeline_foreground_lineage ADD COLUMN timeline_id uuid,
      ADD COLUMN first_event_id bigint,ADD COLUMN head_event_id bigint,ADD COLUMN expires_at timestamptz;
    CREATE TABLE aios.document_section(section_id uuid,node_id uuid);
    CREATE TABLE aios.extracted_sentence(sentence_id uuid,section_id uuid);
    CREATE TABLE aios.claim_candidate(claim_id uuid,sentence_id uuid);
    CREATE TABLE aios.knowledge_acquisition_event(acquisition_id uuid,claim_id uuid,instance_id uuid);
    ALTER TABLE aios.pipeline_job ADD COLUMN attempts integer DEFAULT 0,
      ADD COLUMN created_at timestamptz DEFAULT now(),ADD COLUMN updated_at timestamptz,
      ADD COLUMN worker_id text,ADD COLUMN claimed_at timestamptz,
      ADD COLUMN heartbeat_at timestamptz,ADD COLUMN lease_expires_at timestamptz;
    ''', exec=True)
    active2 = await add_instance(e)
    background = await add_instance(e, age='2 hours', background=True)
    dormant = await add_instance(e, age='2 hours')
    jobs = {}
    for instance in (dormant, background, e.instance, active2):
        jobs[instance] = await e.db.fetchval('''INSERT INTO aios.pipeline_job(job_type,payload,resource_class,run_after)
            VALUES('goal_plan_continuation',$1::jsonb,'LOOKUP',now()) RETURNING job_id''',
            json.dumps({'instance_id': str(instance)}))
    await e.db.execute("UPDATE aios.instance_goal_activity SET last_continuation_started_at=now() WHERE instance_id=$1", e.instance)
    selected = await fetch_next_job(e.db, resource_class='LOOKUP')
    assert selected['job_id'] == jobs[active2]
    await e.db.execute("UPDATE aios.pipeline_job SET status='done' WHERE job_id=$1", selected['job_id'])
    selected = await fetch_next_job(e.db, resource_class='LOOKUP')
    assert selected['job_id'] == jobs[e.instance]
    await e.db.execute("UPDATE aios.pipeline_job SET status='done' WHERE job_id=$1", selected['job_id'])
    selected = await fetch_next_job(e.db, resource_class='LOOKUP', prefer_foreground=False)
    assert selected['job_id'] == jobs[background]
    assert await fetch_next_job(e.db, resource_class='LOOKUP') is None
    inference = await e.db.fetchval(
        """INSERT INTO aios.pipeline_job(job_type,payload,resource_class,run_after)
           VALUES('goal_planning_inference',$1::jsonb,'GLOBAL',now()) RETURNING job_id""",
        json.dumps({'instance_id': str(dormant)}))
    assert await fetch_next_job(e.db, resource_class='GLOBAL') is None
    await observe_interaction(e.db, dormant)
    assert (await fetch_next_job(e.db, resource_class='LOOKUP'))['job_id'] == jobs[dormant]
    assert (await fetch_next_job(e.db, resource_class='GLOBAL'))['job_id'] == inference


@pytest.mark.asyncio
async def test_continuation_admission_rotates_instances_before_multiple_plans(env):
    e = env
    active2 = await add_instance(e)
    background = await add_instance(e, age='2 hours', background=True)
    for instance in (e.instance, active2, background):
        if instance == e.instance:
            goal, attempt = e.goal, e.attempt
        else:
            goal, attempt = uuid4(), uuid4()
            await e.db.execute("INSERT INTO aios.character_agent_goal(goal_id,instance_id,goal_text) VALUES($1,$2,'test')", goal, instance)
            await e.db.execute('INSERT INTO aios.character_goal_attempt(attempt_id,goal_id,instance_id) VALUES($1,$2,$3)', attempt, goal, instance)
        for revision in (1, 2):
            await e.db.execute('''INSERT INTO aios.character_goal_plan(instance_id,goal_id,attempt_id,
                revision,input_fingerprint,source_state_version,status,continuation_check_at)
                VALUES($1,$2,$3,$4,$5,1,'waiting_information',now()-interval '1 minute')''',
                instance, goal, attempt, revision, str(uuid4()))
    assert await e.service.continue_due(limit=2) == 2
    rows = await e.db.fetch("SELECT payload->>'instance_id' AS instance FROM aios.pipeline_job WHERE job_type='goal_plan_continuation'")
    assert {r['instance'] for r in rows} == {str(e.instance), str(active2)}
    assert await e.service.continue_due(limit=2) == 1
    assert await e.db.fetchval("SELECT count(*) FROM aios.pipeline_job WHERE job_type='goal_plan_continuation'") == 3
