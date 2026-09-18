from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from aios_app.epistemic import topology_projection as projection


class FakeFuseki:
    def __init__(self, *, fail_insert_number: int | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail_insert_number = fail_insert_number
        self.insert_count = 0

    def update(self, dataset: str, sparql: str) -> None:
        self.calls.append((dataset, sparql))
        if sparql.startswith("INSERT DATA"):
            self.insert_count += 1
            if self.fail_insert_number == self.insert_count:
                raise RuntimeError("simulated batch failure")


class FakeTopologyModule:
    @staticmethod
    def _rdf_graph(decision):
        return "world", "urn:test:live"

    @staticmethod
    def _json_object(value):
        return dict(value or {})


class FakeDb:
    def __init__(self, nodes):
        self.nodes = nodes

    async def fetch(self, sql, *args):
        if "FROM aios.semantic_topology_node" in sql:
            last_id = args[1]
            rows = self.nodes
            if last_id is not None:
                rows = [row for row in rows if row["topology_node_id"] > last_id]
            return rows[: args[2]]
        if "FROM aios.semantic_topology_edge" in sql:
            return []
        if "FROM aios.semantic_anchor_edge" in sql:
            return []
        raise AssertionError(sql)


class DirtyDb:
    def __init__(self):
        self.executes: list[tuple[str, tuple]] = []

    async def execute_returning_row(self, sql, *args):
        assert "semantic_scope_projection_state" in sql
        return {"dirty_version": 42}

    async def execute(self, sql, *args):
        self.executes.append((sql, args))


class DebounceDb:
    async def fetchrow(self, sql, *args):
        assert "quiet_ready" in sql
        return {
            "scope_key": args[0],
            "scope_kind": "world",
            "dirty_version": 9,
            "projected_version": 8,
            "dirty_at": datetime.now(timezone.utc),
            "quiet_ready": False,
        }


@pytest.fixture(autouse=True)
def topology_module(monkeypatch):
    monkeypatch.setattr(projection, "_TOPOLOGY_MODULE", FakeTopologyModule())


def _node(index: int, label_size: int = 64):
    return {
        "topology_node_id": UUID(int=index + 1),
        "node_type": "TOPIC",
        "node_key": f"node-{index}",
        "label": "x" * label_size,
    }


@pytest.mark.asyncio
async def test_large_projection_is_batched_and_promoted() -> None:
    nodes = [_node(i, label_size=300_000) for i in range(12)]
    db = FakeDb(nodes)
    fuseki = FakeFuseki()
    decision = SimpleNamespace(scope_key="world:test:observed", scope_kind="world")

    dataset, graph = await projection._project_scope_rdf_batched(
        db,
        fuseki,
        decision=decision,
        target_version=7,
    )

    assert dataset == "world"
    assert graph == "urn:test:live"
    inserts = [sparql for _, sparql in fuseki.calls if sparql.startswith("INSERT DATA")]
    assert len(inserts) > 1
    assert max(len(sparql.encode("utf-8")) for sparql in inserts) < 3 * 1024 * 1024
    assert any(
        "MOVE SILENT GRAPH <urn:test:live:staging:7> TO GRAPH <urn:test:live>" in sparql
        for _, sparql in fuseki.calls
    )


@pytest.mark.asyncio
async def test_failed_batch_never_replaces_live_graph() -> None:
    nodes = [_node(i, label_size=300_000) for i in range(12)]
    db = FakeDb(nodes)
    fuseki = FakeFuseki(fail_insert_number=2)
    decision = SimpleNamespace(scope_key="world:test:observed", scope_kind="world")

    with pytest.raises(RuntimeError, match="simulated batch failure"):
        await projection._project_scope_rdf_batched(
            db,
            fuseki,
            decision=decision,
            target_version=8,
        )

    assert not any("MOVE SILENT GRAPH" in sparql for _, sparql in fuseki.calls)
    assert fuseki.calls[-1][1] == "CLEAR SILENT GRAPH <urn:test:live:staging:8>"


@pytest.mark.asyncio
async def test_dirty_scope_pushes_queued_projection_out(monkeypatch) -> None:
    captured = {}

    async def fake_enqueue_job(db, **kwargs):
        captured.update(kwargs)
        return UUID(int=99)

    import aios_app.pipeline.jobs as jobs

    monkeypatch.setattr(jobs, "enqueue_job", fake_enqueue_job)
    db = DirtyDb()
    decision = SimpleNamespace(scope_key="world:test:observed", scope_kind="world")
    before = datetime.now(timezone.utc)

    version = await projection.mark_scope_dirty(db, decision=decision)

    assert version == 42
    assert captured["job_type"] == "project_semantic_scope"
    assert captured["priority"] == projection.RDF_PROJECTION_PRIORITY
    assert captured["run_after"] > before
    assert (
        captured["run_after"] - before
    ).total_seconds() >= projection.RDF_PROJECTION_QUIET_SECONDS - 0.25
    assert any("GREATEST(run_after,$2)" in sql for sql, _ in db.executes)


@pytest.mark.asyncio
async def test_hot_scope_does_not_rebuild_before_quiet_period(monkeypatch) -> None:
    monkeypatch.setattr(projection, "_REAL_PROJECT_SCOPE_RDF", object())
    fuseki = FakeFuseki()

    result = await projection.project_semantic_scope(
        DebounceDb(),
        fuseki,
        scope_key="world:test:observed",
    )

    assert result == {
        "scope_key": "world:test:observed",
        "projected": False,
        "reason": "debouncing",
    }
    assert fuseki.calls == []


class DeltaDb:
    def __init__(self, *, changes, nodes=None, edges=None, anchors=None):
        self.changes = changes
        self.nodes = nodes or []
        self.edges = edges or []
        self.anchors = anchors or []
        self.fetch_calls: list[str] = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append(sql)
        if "FROM aios.semantic_rdf_change" in sql:
            return self.changes
        if "FROM aios.semantic_topology_node" in sql:
            return self.nodes
        if "FROM aios.semantic_topology_edge" in sql:
            return self.edges
        if "FROM aios.semantic_anchor_edge" in sql:
            return self.anchors
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_delta_projection_retracts_deleted_edge() -> None:
    edge_id = UUID(int=101)
    parent_id = UUID(int=102)
    child_id = UUID(int=103)
    db = DeltaDb(
        changes=[
            {
                "change_id": 11,
                "object_kind": "edge",
                "object_id": edge_id,
                "operation": "delete",
                "old_state": {
                    "parent_node_id": str(parent_id),
                    "child_node_id": str(child_id),
                    "edge_type": "supports_belief_atom",
                },
            }
        ]
    )
    fuseki = FakeFuseki()
    decision = SimpleNamespace(scope_key="world:test:observed", scope_kind="world")

    dataset, graph, changed = await projection._project_scope_rdf_delta(
        db,
        fuseki,
        decision=decision,
        from_change_id=10,
        target_change_id=11,
    )

    assert (dataset, graph, changed) == ("world", "urn:test:live", 1)
    assert len(fuseki.calls) == 1
    sparql = fuseki.calls[0][1]
    assert f"urn:aios:topology-edge:{edge_id}" in sparql
    assert "supports_belief_atom" in sparql
    assert "INSERT DATA" not in sparql


@pytest.mark.asyncio
async def test_delta_projection_replaces_current_edge_in_one_batch() -> None:
    edge_id = UUID(int=201)
    parent_id = UUID(int=202)
    child_id = UUID(int=203)
    edge = {
        "edge_id": edge_id,
        "parent_node_id": parent_id,
        "child_node_id": child_id,
        "edge_type": "topic",
        "inference_source": "semantic",
        "inference_status": "accepted",
        "inference_confidence": 0.9,
        "edge_meta": {},
    }
    db = DeltaDb(
        changes=[
            {
                "change_id": 21,
                "object_kind": "edge",
                "object_id": edge_id,
                "operation": "upsert",
                "old_state": {
                    "parent_node_id": str(parent_id),
                    "child_node_id": str(child_id),
                    "edge_type": "topic",
                },
            }
        ],
        edges=[edge],
    )
    fuseki = FakeFuseki()
    decision = SimpleNamespace(scope_key="world:test:observed", scope_kind="world")

    _, _, changed = await projection._project_scope_rdf_delta(
        db,
        fuseki,
        decision=decision,
        from_change_id=20,
        target_change_id=21,
    )

    assert changed == 1
    assert len(fuseki.calls) == 1
    sparql = fuseki.calls[0][1]
    assert sparql.startswith("DELETE WHERE")
    assert "INSERT DATA" in sparql
    assert f"urn:aios:topology-edge:{edge_id}" in sparql
    assert sum("FROM aios.semantic_topology_edge" in sql for sql in db.fetch_calls) == 1


@pytest.mark.asyncio
async def test_delta_projection_preserves_all_old_edge_coordinates() -> None:
    edge_id = UUID(int=301)
    first_parent = UUID(int=302)
    second_parent = UUID(int=303)
    child_id = UUID(int=304)
    edge = {
        "edge_id": edge_id,
        "parent_node_id": second_parent,
        "child_node_id": child_id,
        "edge_type": "bar",
        "inference_source": "semantic",
        "inference_status": "accepted",
        "inference_confidence": None,
        "edge_meta": {},
    }
    db = DeltaDb(
        changes=[
            {
                "change_id": 31,
                "object_kind": "edge",
                "object_id": edge_id,
                "operation": "upsert",
                "old_state": {
                    "parent_node_id": str(first_parent),
                    "child_node_id": str(child_id),
                    "edge_type": "foo",
                },
            },
            {
                "change_id": 32,
                "object_kind": "edge",
                "object_id": edge_id,
                "operation": "upsert",
                "old_state": {
                    "parent_node_id": str(second_parent),
                    "child_node_id": str(child_id),
                    "edge_type": "foo",
                },
            },
        ],
        edges=[edge],
    )
    fuseki = FakeFuseki()
    decision = SimpleNamespace(scope_key="world:test:observed", scope_kind="world")

    _, _, changed = await projection._project_scope_rdf_delta(
        db,
        fuseki,
        decision=decision,
        from_change_id=30,
        target_change_id=32,
    )

    assert changed == 1
    assert len(fuseki.calls) == 1
    sparql = fuseki.calls[0][1]
    assert str(first_parent) in sparql
    assert str(second_parent) in sparql
    assert "topology#foo" in sparql
    assert "topology#bar" in sparql
    assert sparql.count("INSERT DATA") == 1


@pytest.mark.asyncio
async def test_many_delta_objects_use_bounded_batches_and_bulk_fetch(monkeypatch) -> None:
    monkeypatch.setattr(projection, "RDF_DELTA_TARGET_OBJECTS", 8)
    changes = []
    edges = []
    for index in range(20):
        edge_id = UUID(int=400 + index)
        parent_id = UUID(int=500 + index)
        child_id = UUID(int=600 + index)
        changes.append(
            {
                "change_id": 100 + index,
                "object_kind": "edge",
                "object_id": edge_id,
                "operation": "upsert",
                "old_state": {
                    "parent_node_id": str(parent_id),
                    "child_node_id": str(child_id),
                    "edge_type": "topic",
                },
            }
        )
        edges.append(
            {
                "edge_id": edge_id,
                "parent_node_id": parent_id,
                "child_node_id": child_id,
                "edge_type": "topic",
                "inference_source": "semantic",
                "inference_status": "accepted",
                "inference_confidence": 0.8,
                "edge_meta": {},
            }
        )

    db = DeltaDb(changes=changes, edges=edges)
    fuseki = FakeFuseki()
    decision = SimpleNamespace(scope_key="world:test:observed", scope_kind="world")

    _, _, changed = await projection._project_scope_rdf_delta(
        db,
        fuseki,
        decision=decision,
        from_change_id=99,
        target_change_id=119,
    )

    assert changed == 20
    assert len(fuseki.calls) == 3
    assert sum("FROM aios.semantic_topology_edge" in sql for sql in db.fetch_calls) == 1
    assert all("INSERT DATA" in sparql for _, sparql in fuseki.calls)

