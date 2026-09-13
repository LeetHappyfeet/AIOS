from uuid import uuid4

import pytest

from aios_app.semantic_index.query import SemanticQueryService
from aios_app.world.retrieval_scope import build_retrieval_scope


class FakeDB:
    def __init__(self):
        self.local = uuid4()
        self.parent = uuid4()
        self.baseline = uuid4()
        self.calls = 0

    async def fetch(self, sql, *args):
        self.calls += 1
        assert "get_world_resolution_scope" in sql
        assert args[0] == self.local
        assert args[1] == "geography"
        return [
            {"source_world_id": self.local, "priority": 0, "depth": 0},
            {"source_world_id": self.parent, "priority": 100, "depth": 1},
            {"source_world_id": self.baseline, "priority": 200, "depth": 2},
        ]


@pytest.mark.asyncio
async def test_scope_filter_groups_materialized_worlds_into_lazy_stages():
    db = FakeDB()
    scope = await build_retrieval_scope(
        db,
        world_id=db.local,
        domain="geography",
    )
    assert db.calls == 1
    assert scope.qdrant_world_stages == (
        (str(db.local),),
        (str(db.parent),),
        (str(db.baseline),),
    )
    assert scope.all_world_ids == (db.local, db.parent, db.baseline)


def test_semantic_client_sends_world_stages_before_vector_search(monkeypatch):
    service = SemanticQueryService.__new__(SemanticQueryService)
    captured = {}

    def fake_request(payload):
        captured.update(payload)
        return []

    service._request = fake_request
    service.search_epistemic_staged(
        "Brooklyn",
        character_id="Captain_America",
        instance_ids=[uuid4()],
        world_stages=[["marvel-rp"], ["marvel"], ["baseline"]],
        min_hits=5,
    )

    assert captured["op"] == "search_epistemic_staged"
    assert captured["world_stages"] == [
        ["marvel-rp"],
        ["marvel"],
        ["baseline"],
    ]
    assert captured["min_hits"] == 5


def test_empty_world_stage_never_widens_to_unfiltered_semantic_search(monkeypatch):
    service = SemanticQueryService.__new__(SemanticQueryService)

    def fail_request(payload):
        raise AssertionError("empty scope must not issue an unfiltered request")

    service._request = fail_request
    assert service.search_epistemic_staged(
        "anything",
        character_id="x",
        instance_ids=[],
        world_stages=[],
    ) == []
