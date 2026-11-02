import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from test_goal_planning_postgres import env
from aios_app.pipeline.telemetry import STAGES_SQL, RATES_SQL, lane_totals

pytestmark = pytest.mark.skipif(not os.getenv('AIOS_PGLITE_MODULE'), reason='PGlite not configured')
ROOT = Path(__file__).resolve().parents[1]


async def migrate(e):
    await e.db.pool.con.query('''ALTER TABLE aios.pipeline_job
        ADD COLUMN created_at timestamptz DEFAULT now(),
        ADD COLUMN updated_at timestamptz DEFAULT now();''', exec=True)
    sql = (ROOT/'migrations/current/20261101_02_pipeline_liveness.sql').read_text()
    await e.db.pool.con.query(sql, exec=True)


@pytest.mark.asyncio
async def test_null_expiry_is_explicit_false_and_parked_age_does_not_count_as_lag(env):
    e = env
    await migrate(e)
    await e.db.execute("UPDATE aios.instance_goal_activity SET last_interaction_at=now()-interval '2 days'")
    assert await e.db.fetchval('SELECT eligible FROM aios.instance_goal_scheduling') is False
    await e.db.execute('''INSERT INTO aios.pipeline_job(job_type,payload,resource_class,scheduling_lane,run_after,created_at)
        VALUES('goal_plan_continuation',$1::jsonb,'LOOKUP','BACKGROUND',now(),now()-interval '2 days')''',
        json.dumps({'instance_id': str(e.instance)}))
    stages = await e.db.fetch(STAGES_SQL)
    assert stages[0]['queued'] == 0 and stages[0]['parked'] == 1 and stages[0]['oldest_seconds'] == 0
    await e.db.execute("UPDATE aios.instance_goal_activity SET last_interaction_at=now()")
    stages = await e.db.fetch(STAGES_SQL)
    assert stages[0]['queued'] == 1 and stages[0]['parked'] == 0
    await e.db.execute("UPDATE aios.pipeline_job SET run_after=now()+interval '2 minutes'")
    stages = await e.db.fetch(STAGES_SQL)
    assert stages[0]['delayed'] == 1 and stages[0]['queued'] == 0
    await e.db.execute("UPDATE aios.character_agent_runtime SET state='paused'")
    assert await e.db.fetchval('SELECT eligible FROM aios.instance_goal_scheduling') is False


@pytest.mark.asyncio
async def test_pending_classification_and_rates_ignore_older_history(env):
    e = env
    await migrate(e)
    for status, age in [('queued','4 seconds'), ('done','2 days'), ('done','1 second'), ('failed','1 second')]:
        await e.db.execute('''INSERT INTO aios.pipeline_job(job_type,payload,status,resource_class,scheduling_lane,
              run_after,created_at,updated_at) VALUES('test','{}',$1,'FAST_SQL','LIVE',now(),now()-$2::interval,now()-$2::interval)''', status, age)
    await e.db.execute('''INSERT INTO aios.pipeline_job(job_type,payload,status,resource_class,scheduling_lane,run_after)
          VALUES('goal_plan_continuation',$1::jsonb,'running','LOOKUP','BACKGROUND',now()),
                ('goal_plan_continuation',$1::jsonb,'queued','LOOKUP','BACKGROUND',now())''',
          json.dumps({'instance_id': str(e.instance)}))
    stages = await e.db.fetch(STAGES_SQL)
    lookup = next(r for r in stages if r['resource_class'] == 'LOOKUP')
    assert lookup['running'] == 1 and lookup['waiting'] == 1 and lookup['queued'] == 0
    lanes = lane_totals(stages)
    assert sum(r['queued'] for r in lanes) == 1 and sum(r['running'] for r in lanes) == 1
    rates = await e.db.fetchrow(RATES_SQL)
    assert rates['arrivals_30s'] == 5 and rates['done_30s'] == 1 and rates['failed_60s'] == 1
    assert rates['live_arrivals_30s'] == 3 and rates['live_done_30s'] == 1
    indexes = await e.db.fetch("SELECT indexname FROM pg_indexes WHERE schemaname='aios'")
    assert {'idx_pipeline_telemetry_arrivals', 'idx_pipeline_telemetry_completed', 'idx_pipeline_telemetry_failed'} <= {r['indexname'] for r in indexes}


@pytest.mark.asyncio
async def test_expired_heartbeat_cannot_resurrect_a_lease(env):
    from aios_app.pipeline.jobs import heartbeat_job
    e = env
    await e.db.pool.con.query('''ALTER TABLE aios.pipeline_job
         ADD COLUMN worker_id text,ADD COLUMN heartbeat_at timestamptz,
         ADD COLUMN lease_expires_at timestamptz,ADD COLUMN updated_at timestamptz;''', exec=True)
    job = uuid4()
    await e.db.execute('''INSERT INTO aios.pipeline_job(job_id,status,worker_id,lease_expires_at)
          VALUES($1,'running','worker',now()-interval '1 second')''', job)
    assert not await heartbeat_job(e.db, job_id=job, worker_id='worker', lease_seconds=120)
    await e.db.execute("UPDATE aios.pipeline_job SET lease_expires_at=now()+interval '60 seconds'")
    assert not await heartbeat_job(e.db, job_id=job, worker_id='replacement', lease_seconds=120)
    assert await heartbeat_job(e.db, job_id=job, worker_id='worker', lease_seconds=120)
