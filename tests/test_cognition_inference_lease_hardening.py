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
