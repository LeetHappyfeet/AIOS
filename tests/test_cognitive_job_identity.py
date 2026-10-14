"""Real enqueue SQL and released queue indexes, using isolated PostgreSQL."""
import os
from pathlib import Path
from uuid import uuid4

import pytest

from aios_app.pipeline.jobs import enqueue_job
from test_goal_planning_postgres import env

pytestmark = pytest.mark.skipif(not os.getenv("AIOS_PGLITE_MODULE"), reason="PGlite not configured")
ROOT = Path(__file__).resolve().parents[1]


async def old_index(db):
    await db.pool.con.query((ROOT/'migrations/current/20261003_02_rdf_scope_job_uniqueness.sql').read_text(),exec=True)


async def fix_index(db):
    await db.pool.con.query((ROOT/'migrations/current/20261014_03_cognitive_job_identity.sql').read_text(),exec=True)


@pytest.mark.asyncio
async def test_reproduces_global_collision_then_allows_distinct_operation_jobs(env):
    db = env.db
    await old_index(db)
    first = {'instance_id':str(uuid4()),'operation_id':str(uuid4())}
    second = {'instance_id':str(uuid4()),'operation_id':str(uuid4())}
    assert await enqueue_job(db,job_type='cognitive_operation',payload=first)
    assert await enqueue_job(db,job_type='cognitive_operation',payload=second) is None
    await fix_index(db)
    assert await enqueue_job(db,job_type='cognitive_operation',payload=second)
    # Multiple operations within ONE instance are also distinct work.
    third = {**first,'operation_id':str(uuid4())}
    assert await enqueue_job(db,job_type='cognitive_operation',payload=third)
    assert await enqueue_job(db,job_type='cognitive_operation',payload=first) is None
    assert await db.fetchval('SELECT count(*) FROM aios.pipeline_job') == 3


@pytest.mark.asyncio
async def test_inference_transaction_task_instance_and_global_dedup(env):
    db = env.db
    await old_index(db)
    await fix_index(db)
    instance = str(uuid4())
    for kind,key in [('goal_planning_inference','operation_id'),
                     ('internal_cognition_inference','transaction_id'),
                     ('agent_task','task_id'),('agent_wake','instance_id')]:
        first = {'instance_id':instance,key:str(uuid4())}
        second = {**first,key:str(uuid4())}
        assert await enqueue_job(db,job_type=kind,payload=first)
        assert await enqueue_job(db,job_type=kind,payload=second)
        assert await enqueue_job(db,job_type=kind,payload=first) is None
    assert await enqueue_job(db,job_type='global_maintenance',payload={})
    assert await enqueue_job(db,job_type='global_maintenance',payload={}) is None


@pytest.mark.asyncio
async def test_real_plan_admission_and_inference_enqueue_are_not_global_singletons(env):
    e = env
    await old_index(e.db)
    await fix_index(e.db)
    e.monkeypatch.setattr('aios_app.agent.goal_planning.enqueue_job',enqueue_job)
    # An unrelated character already has both types of work in flight.
    other={'instance_id':str(uuid4()),'operation_id':str(uuid4())}
    assert await enqueue_job(e.db,job_type='cognitive_operation',payload=other)
    assert await enqueue_job(e.db,job_type='goal_planning_inference',payload=other)
    assert await e.service.schedule(e.instance,e.goal)
    from aios_app.agent.cognitive_operations import CognitiveOperationEngine
    op = await e.db.fetchval('SELECT operation_id FROM aios.character_cognitive_operation')
    await CognitiveOperationEngine(e.db).execute(op)
    rows = await e.db.fetch("SELECT * FROM aios.pipeline_job WHERE payload->>'operation_id'=$1",str(op))
    assert {r['job_type'] for r in rows} == {'cognitive_operation','goal_planning_inference'}


@pytest.mark.asyncio
async def test_migration_requeues_only_specific_enqueue_failure_preserving_error(env):
    e=env
    assert await e.service.schedule(e.instance,e.goal)
    await e.db.execute("UPDATE aios.character_goal_plan SET status='failed'")
    await e.db.execute('''UPDATE aios.character_cognitive_operation SET status='failed',
        result='{"error":"could not enqueue goal planning","error_type":"RuntimeError"}',
        completed_at=now(),reconciled_at=now()''')
    await old_index(e.db)
    await fix_index(e.db)
    assert await e.db.fetchval('SELECT status FROM aios.character_goal_plan')=='queued'
    assert await e.db.fetchval('SELECT status FROM aios.character_cognitive_operation')=='queued'
    assert await e.db.fetchval("SELECT input->'enqueue_repair'->>'error' FROM aios.character_cognitive_operation")=='could not enqueue goal planning'
    assert await e.db.fetchval('SELECT reconciled_at FROM aios.character_cognitive_operation') is None


@pytest.mark.asyncio
async def test_migration_does_not_retry_model_or_validation_failures(env):
    e=env
    assert await e.service.schedule(e.instance,e.goal)
    await e.db.execute("UPDATE aios.character_goal_plan SET status='failed'")
    await e.db.execute('''UPDATE aios.character_cognitive_operation SET status='failed',
        result='{"error":"invalid plan fields","error_type":"ValueError"}' ''')
    await old_index(e.db)
    await fix_index(e.db)
    assert await e.db.fetchval('SELECT status FROM aios.character_goal_plan')=='failed'
    assert await e.db.fetchval('SELECT status FROM aios.character_cognitive_operation')=='failed'
