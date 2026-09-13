from types import SimpleNamespace

import pytest

from aios_app.epistemic.halo_retrieval import (
    HALO_PREDECESSOR_NODES,
    TopologyRetriever,
)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


@pytest.mark.asyncio
async def test_source_halo_uses_only_predecessors_on_active_source_timeline():
    db = FakeDB(
        [
            {"node_id": "n3", "event_id": 3, "message_text": "third"},
            {"node_id": "n2", "event_id": 2, "message_text": "second"},
            {"node_id": "n1", "event_id": 1, "message_text": "first"},
        ]
    )
    retriever = TopologyRetriever(db)
    context = SimpleNamespace(
        source_timeline_id="source-timeline",
        source_head_node_id="head-node",
        timeline_id="runtime-timeline",
        head_node_id="runtime-head",
    )

    halo_text, node_ids = await retriever._dag_halo(context)

    assert halo_text == "first second third"
    assert node_ids == ("n3", "n2", "n1")
    sql, args = db.calls[0]
    assert "dn.timeline_id=$1" in sql
    assert "dn.event_id < head.event_id" in sql
    assert "ie.superseded_at IS NULL" in sql
    assert args == ("source-timeline", "head-node", HALO_PREDECESSOR_NODES)


@pytest.mark.asyncio
async def test_halo_falls_back_to_runtime_timeline_without_source_cursor():
    db = FakeDB(
        [{"node_id": "r1", "event_id": 1, "message_text": "runtime context"}]
    )
    retriever = TopologyRetriever(db)
    context = SimpleNamespace(
        source_timeline_id=None,
        source_head_node_id=None,
        timeline_id="runtime-timeline",
        head_node_id="runtime-head",
    )

    halo_text, node_ids = await retriever._dag_halo(context)

    assert halo_text == "runtime context"
    assert node_ids == ("r1",)
    sql, args = db.calls[0]
    assert "dn.timeline_id=$1" in sql
    assert "dn.event_id < head.event_id" in sql
    assert "ingest_event" not in sql
    assert args == ("runtime-timeline", "runtime-head", HALO_PREDECESSOR_NODES)


@pytest.mark.asyncio
async def test_halo_is_cached_per_timeline_and_head():
    db = FakeDB(
        [{"node_id": "n1", "event_id": 1, "message_text": "cached"}]
    )
    retriever = TopologyRetriever(db)
    context = SimpleNamespace(
        source_timeline_id="source-timeline",
        source_head_node_id="head-node",
        timeline_id="runtime-timeline",
        head_node_id="runtime-head",
    )

    first = await retriever._dag_halo(context)
    second = await retriever._dag_halo(context)

    assert first == second
    assert len(db.calls) == 1
