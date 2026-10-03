"""Source DAG references are not character cognitive task IDs."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aios_app.inference.broker import (
    InferenceBroker, InferencePersistenceError, InferenceProviderExecutionError,
    InferenceRequest,
)


class RejectingDB:
    def __init__(self):
        self.arguments = None
        self.query = None

    async def execute_returning_row(self, query, *args):
        self.query, self.arguments = query, args
        raise RuntimeError("simulated FK or missing migration")


class Store:
    def __init__(self, provider):
        self.provider = provider
        self.health_events = []

    async def routable(self, worker_class):
        return [self.provider]

    async def record_health(self, provider_id, **kwargs):
        self.health_events.append((provider_id, kwargs))


def test_source_node_does_not_fill_task_fk_and_db_fault_is_not_provider_failure():
    instance, node, provider_id = uuid4(), uuid4(), uuid4()
    db = RejectingDB()
    provider = SimpleNamespace(provider_id=provider_id, provider_key="Provider_A", model="Model_A")
    broker = InferenceBroker(db)
    store = Store(provider)
    broker.providers = store
    request = InferenceRequest(
        instance_id=instance, worker_class="message_cognition",
        prompt="Return a JSON object", source_node_id=node,
    )
    with pytest.raises(InferencePersistenceError, match="could not persist"):
        asyncio.run(broker.infer(request))
    assert db.arguments[0] == instance
    assert db.arguments[1] is None  # task_id is reserved for character_cognitive_task
    assert db.arguments[-1] == node
    assert "source_node_id" in db.query
    assert "previous.source_node_id=$11::uuid" in db.query
    assert not store.health_events


def test_real_cognitive_task_identity_remains_independent():
    instance, node, task = uuid4(), uuid4(), uuid4()
    request = InferenceRequest(
        instance_id=instance,worker_class="message_cognition",prompt="Inspect",
        task_id=task,source_node_id=node,
    )
    assert request.task_id == task
    assert request.source_node_id == node
    assert request.task_id != request.source_node_id


def test_remote_execution_failure_still_marks_provider_unhealthy():
    instance = uuid4()
    provider = SimpleNamespace(provider_id=uuid4(), provider_key="Provider_B", model="Model_B")
    broker = InferenceBroker(RejectingDB())
    store = Store(provider)
    broker.providers = store

    async def remote_failure(*_args):
        raise InferenceProviderExecutionError("HTTP Error 503")

    broker._attempt = remote_failure
    from aios_app.inference.broker import InferenceUnavailable
    with pytest.raises(InferenceUnavailable):
        asyncio.run(broker.infer(InferenceRequest(
            instance_id=instance,worker_class="message_cognition",prompt="Inspect"
        )))
    assert store.health_events[0][1]["ok"] is False


def test_source_node_migration_preserves_cognitive_task_reference():
    root = Path(__file__).resolve().parents[1]
    sql = (root / "migrations/current/20261003_05_inference_source_node.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS source_node_id uuid" in sql
    assert "REFERENCES aios.dag_node(node_id)" in sql
    assert "DROP" not in sql
    base = (root / "migrations/current/20260923_07_inference_workers.sql").read_text()
    assert "REFERENCES aios.character_cognitive_task(task_id)" in base
