from __future__ import annotations

import asyncio
from uuid import uuid4

from aios_app.rdf.liminal_compaction import compact_liminal_claims


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    async def fetch(self, query, *args):
        assert "liminal_compaction_candidate" in query
        return self.rows

    async def execute(self, query, *args):
        self.executed.append((query, args))


class FakeFuseki:
    def __init__(self):
        self.updates = []

    def update(self, dataset, sparql):
        self.updates.append((dataset, sparql))


def test_compaction_removes_only_liminal_projection_and_marks_receipt():
    claim_a = uuid4()
    claim_b = uuid4()
    db = FakeDB(
        [
            {
                "claim_id": claim_a,
                "lifecycle_status": "absorbed",
                "absorbed_into_surface_key": "char:Shego:atom:a",
                "reason": "represented_by_character_memory",
            },
            {
                "claim_id": claim_b,
                "lifecycle_status": "promoted",
                "absorbed_into_surface_key": "world:w:atom:b",
                "reason": "represented_by_world_memory",
            },
        ]
    )
    fuseki = FakeFuseki()

    count = asyncio.run(compact_liminal_claims(db, fuseki, batch_size=50))

    assert count == 2
    assert len(fuseki.updates) == 1
    dataset, sparql = fuseki.updates[0]
    assert dataset == "world"
    assert "urn:aios:world:liminal" in sparql
    assert f"urn:aios:world:claim:{claim_a}" in sparql
    assert f"urn:aios:world:claim:{claim_b}" in sparql
    assert "CLEAR" not in sparql

    assert len(db.executed) == 1
    query, args = db.executed[0]
    assert "rdf_promotion_log" in query
    assert "compacted_at" in query
    assert args[0] == [claim_a, claim_b]


def test_compaction_is_noop_without_terminal_claims():
    db = FakeDB([])
    fuseki = FakeFuseki()

    count = asyncio.run(compact_liminal_claims(db, fuseki))

    assert count == 0
    assert fuseki.updates == []
    assert db.executed == []
