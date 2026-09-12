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
async def test_large_projection_is_batched_and_promoted(monkeypatch) -> None:
    monkeypatch.setattr(projection, "RDF_UPDATE_TARGET_BYTES", 1024)
    nodes = [_node(i, label_size=300) for i in range(12)]
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
    assert any("COPY SILENT GRAPH <urn:test:live:staging:7> TO GRAPH <urn:test:live>" in sparql for _, sparql in fuseki.calls)


@pytest.mark.asyncio
async def test_failed_batch_never_replaces_live_graph(monkeypatch) -> None:
    monkeypatch.setattr(projection, "RDF_UPDATE_TARGET_BYTES", 1024)
    nodes = [_node(i, label_size=300) for i in range(12)]
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

    assert not any("COPY SILENT GRAPH" in sparql for _, sparql in fuseki.calls)
    assert fuseki.calls[-1][1] == "CLEAR SILENT GRAPH <urn:test:live:staging:8>"
