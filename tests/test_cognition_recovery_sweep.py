"""Restart-independent historical cognition recovery (no live database required)."""
import asyncio
import inspect
from uuid import uuid4

import pytest

from aios_app.epistemic import cognition_recovery_sweep
from aios_app import runner_v2
from aios_app.pipeline import jobs


class ScanDB:
    def __init__(self, rows=()):
        self.rows = rows
        self.sql = ""
        self.args = ()

    async def fetch(self, sql, *args):
        self.sql, self.args = sql, args
        return list(self.rows)


def test_discovery_is_current_branch_ancestor_and_pending_only():
    db = ScanDB()
    assert asyncio.run(cognition_recovery_sweep.discover_abandoned_enrichment(
        db,limit=8,
    )) == []
    assert db.args == (8,)
    assert "WITH RECURSIVE pending_instances" in db.sql
    assert "rs.source_head_node_id" in db.sql
    assert "rs.source_timeline_id" in db.sql
    assert "parent.node_id=de.parent_node_id" in db.sql
    assert "NOT parent.node_id=ANY(chain.visited)" in db.sql
    assert "chain.depth<4096" in db.sql
    assert "chain.node_id=c.node_id" in db.sql
    assert "c.summary->>'historical_catchup'='true'" in db.sql
    assert "c.summary->>'enrichment_pending'='true'" in db.sql
    assert "c.summary->>'enrichment_deferred'='true'" in db.sql


def test_discovery_excludes_active_leases_and_queued_running_jobs():
    db=ScanDB()
    asyncio.run(cognition_recovery_sweep.discover_abandoned_enrichment(db))
    assert "ir.worker_class='message_cognition' AND ir.status='running'" in db.sql
    assert "ir.lease_expires_at > now()" in db.sql
    assert "ir.created_at > now()-interval '2 minutes'" in db.sql
    assert "pj.status IN ('queued','running')" in db.sql
    assert "'message_cognition_catchup'" in db.sql
    assert "'message_cognition_enrichment'" in db.sql
    assert "LEAST(900, 60 * power(2, LEAST(last.recent_failures,4)))" in db.sql


@pytest.mark.parametrize("limit",[-1,0,17,100])
def test_sweep_rejects_unbounded_limit(limit):
    with pytest.raises(ValueError):
        asyncio.run(cognition_recovery_sweep.discover_abandoned_enrichment(
            ScanDB(),limit=limit,
        ))


def test_sweep_enqueues_only_discovered_instances_via_blessed_api(monkeypatch):
    one, two=uuid4(),uuid4()
    db=ScanDB([{"instance_id":one,"oldest_pending_event":7},
               {"instance_id":two,"oldest_pending_event":11}])
    queued=[]
    async def enqueue(db_arg, **kwargs):
        assert db_arg is db
        queued.append(kwargs)
        return uuid4() if len(queued)==1 else None
    monkeypatch.setattr(cognition_recovery_sweep,"enqueue_job",enqueue)
    assert asyncio.run(cognition_recovery_sweep.enqueue_abandoned_enrichment(
        db,limit=2,
    )) == 1
    assert len(queued)==2
    assert queued[0]["job_type"]=="message_cognition_catchup"
    assert queued[0]["payload"]["instance_id"]==str(one)
    assert queued[0]["payload"]["recovery_source"]=="deferred_enrichment_sweep"
    assert queued[0]["priority"]==65


def test_atomic_enqueue_dedupes_queued_only_and_has_no_global_lock():
    sql=inspect.getsource(jobs.enqueue_job)
    assert "pg_advisory_xact_lock" in sql
    assert "CASE WHEN $1='message_cognition_catchup'" in sql
    assert "existing.payload->>'instance_id'" in sql
    assert "existing.status='queued'" in sql
    # A running handler must be able to enqueue its future continuation.


def test_recovery_loop_is_attached_to_runner_start_and_shutdown():
    source=inspect.getsource(runner_v2.run_runner)
    assert "_deferred_cognition_recovery_loop()" in source
    assert "cognition_recovery.cancel()" in source
    assert "projector, telemetry, cognition_recovery" in source
    sweep=inspect.getsource(runner_v2._deferred_cognition_recovery_loop)
    assert "reap_stale_requests()" in sweep
    assert "enqueue_abandoned_enrichment(db, limit=16)" in sweep
    assert "await asyncio.sleep(interval)" in sweep
