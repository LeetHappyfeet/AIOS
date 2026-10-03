"""Historical inference leasing must be safe across network and process failures."""
import asyncio
from uuid import uuid4

import pytest

from aios_app.epistemic import cognition_catchup
from aios_app.inference import broker


class FakeProviderStore:
    calls = 0

    def __init__(self, db):
        self.db = db

    async def reap_stale_requests(self):
        self.calls += 1
        return getattr(self.db, "expired", 0)


class FakeDB:
    def __init__(self, *, active=None, expired=0, latest=None):
        self.active = active
        self.expired = expired
        self.latest = latest
        self.queries = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "status='running'" in query:
            return self.active
        if "created_at >= now()" in query:
            return self.latest
        raise AssertionError(query)


def test_running_lease_blocks_duplicate_historical_inference(monkeypatch):
    instance, node, request = uuid4(), uuid4(), uuid4()
    db = FakeDB(active={"request_id":request, "task_id":None, "status":"running",
                        "created_at":"recent", "lease_expires_at":"future",
                        "last_progress_at":"recent"})
    calls = []
    async def missing(*_a, **_kw):
        return []
    async def deferred(*_a, **_kw):
        return [{"node_id":node,"event_id":20}]
    class NeverEnricher:
        def __init__(self, _db):
            calls.append("constructed")
        async def run(self, **kwargs):
            calls.append("inferred")
    monkeypatch.setattr(cognition_catchup, "missing_cognition_nodes", missing)
    monkeypatch.setattr(cognition_catchup, "deferred_enrichment_nodes", deferred)
    from aios_app.inference import providers
    monkeypatch.setattr(providers, "InferenceProviderStore", FakeProviderStore)
    from aios_app.epistemic import message_cognition_enrichment
    monkeypatch.setattr(message_cognition_enrichment, "MessageCognitionEnricher", NeverEnricher)
    result = asyncio.run(cognition_catchup.finish_deferred_cognition(
        db, instance_id=instance,
    ))
    assert result["status"] == "source_inference_running"
    assert result["latest_inference"]["request_id"] == str(request)
    assert result["latest_inference"]["task_id"] is None
    assert result["goal_reconciliation"] is None
    assert calls == []
    assert any("lease_expires_at IS NULL OR lease_expires_at > now()" in q for q, _ in db.queries)


@pytest.mark.parametrize("status,expected", [
    ("running","source_inference_running"),
    ("invalid","source_inference_invalid_response"),
    ("failed","source_inference_failed"),
])
def test_pending_receipt_exposes_latest_attempt_type(monkeypatch, status, expected):
    instance, node, request = uuid4(), uuid4(), uuid4()
    latest={"request_id":request,"status":status,"validation_error":None,
            "error":None,"created_at":"recent"}
    db=FakeDB(latest=latest,expired=1)
    async def missing(*_a,**_kw):
        return []
    async def deferred(*_a,**_kw):
        return [{"node_id":node,"event_id":20}]
    class Enricher:
        def __init__(self,_db): pass
        async def run(self,**_kw): return 0
    monkeypatch.setattr(cognition_catchup,"missing_cognition_nodes",missing)
    monkeypatch.setattr(cognition_catchup,"deferred_enrichment_nodes",deferred)
    from aios_app.inference import providers
    monkeypatch.setattr(providers,"InferenceProviderStore",FakeProviderStore)
    from aios_app.epistemic import message_cognition_enrichment
    monkeypatch.setattr(message_cognition_enrichment,"MessageCognitionEnricher",Enricher)
    result=asyncio.run(cognition_catchup.finish_deferred_cognition(db,instance_id=instance))
    assert result["status"]==expected
    assert result["reaped_expired_requests"]==1
    assert result["goal_reconciliation"] is None


def test_broker_sql_scopes_active_task_uniqueness_to_instance_and_worker():
    import inspect
    source=inspect.getsource(broker.InferenceBroker._attempt)
    assert "previous.task_id=$2::uuid" in source
    assert "previous.instance_id=$1 AND previous.worker_class=$4" in source
    assert "previous.lease_expires_at > now()" in source


def test_enricher_assigns_node_task_id_and_checks_existing_lease():
    import inspect
    from aios_app.epistemic.message_cognition_enrichment import MessageCognitionEnricher
    source=inspect.getsource(MessageCognitionEnricher.run)
    assert "task_id=node_id" in source
    assert "AND task_id=$2 AND status='running'" in source
    assert "lease_expires_at IS NULL OR lease_expires_at > now()" in source



def test_scheduler_queues_delayed_retry_without_creating_second_active_inference(monkeypatch):
    from aios_app import runner_v2
    instance=uuid4()
    calls=[]
    class DB:
        async def fetchrow(self,sql,*args):
            assert "status='queued'" in sql
            return None
    async def recovery(_db,*,instance_id,limit):
        return {"more_possible":False,"committed":0}
    async def finish(_db,*,instance_id):
        return {"status":"source_inference_running","enrichment_completed":0}
    async def enqueue(_db,**kwargs):
        calls.append(kwargs)
    from aios_app.epistemic import cognition_catchup
    monkeypatch.setattr(cognition_catchup,"recover_missing_cognition",recovery)
    monkeypatch.setattr(cognition_catchup,"finish_deferred_cognition",finish)
    monkeypatch.setattr(runner_v2,"enqueue_job",enqueue)
    asyncio.run(runner_v2.handle_message_cognition_catchup(
        DB(),{"payload":{"instance_id":str(instance),"retry_count":1}},
    ))
    assert len(calls)==1
    assert calls[0]["job_type"]=="message_cognition_catchup"
    assert calls[0]["payload"]["retry_count"]==2
    assert calls[0]["run_after"] is not None


def test_scheduler_does_not_duplicate_queued_followup(monkeypatch):
    from aios_app import runner_v2
    instance=uuid4()
    class DB:
        async def fetchrow(self,sql,*args):
            return {"job_id":uuid4()}
    async def recovery(*_args,**_kwargs):
        return {"more_possible":False,"committed":0}
    async def finish(*_args,**_kwargs):
        return {"status":"source_inference_unavailable","enrichment_completed":0}
    async def enqueue(*_args,**_kwargs):
        raise AssertionError("already queued")
    monkeypatch.setattr(cognition_catchup,"recover_missing_cognition",recovery)
    monkeypatch.setattr(cognition_catchup,"finish_deferred_cognition",finish)
    monkeypatch.setattr(runner_v2,"enqueue_job",enqueue)
    asyncio.run(runner_v2.handle_message_cognition_catchup(
        DB(),{"payload":{"instance_id":str(instance)}},
    ))
