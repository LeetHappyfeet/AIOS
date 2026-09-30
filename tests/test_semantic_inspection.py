import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from aios_app.semantic_index.inspection import representatives, inspect_neighborhood
from aios_app.semantic_index.config import SemanticIndexConfig


def test_cosine_medoid_uses_original_vectors_and_diverse_extremes():
    result = representatives({"a": [1,-.3], "b": [1,0], "c": [1,.3], "d": [-1,0]}, limit=2)
    assert result["medoid_id"] == "b"
    assert result["representative_ids"] == ["b","d"]


def test_core_medoid_and_invalid_vectors():
    result = representatives({"a":[1,0],"b":[1,.2],"c":[1,0],"zero":[0,0]}, core_ids={"b"})
    assert result["medoid_id"] == "b"
    assert result["vector_count"] == 3


@pytest.mark.parametrize("kwargs", [{}, {"point_ids":[UUID(int=1)],"cluster_id":UUID(int=2)},
                                   {"point_ids":[UUID(int=1)],"collection":"invalid"},
                                   {"point_ids":[UUID(int=1)],"limit":513}])
def test_invalid_inspection_inputs_fail_before_io(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(inspect_neighborhood(None, **kwargs))


def test_ownership_copies_are_deduplicated_and_source_evidence_preserved(monkeypatch):
    from aios_app.semantic_index import inspection
    proposition = UUID(int=3)
    points = [SimpleNamespace(id=UUID(int=n), payload={"proposition_id":str(proposition),"instance_id":str(n)}, vector=[1,0]) for n in [1,2]]
    class Client:
        def retrieve(self, **kwargs): return points
        def close(self): pass
    monkeypatch.setattr(inspection, "QdrantStore", lambda *args: SimpleNamespace(client=Client()))
    class DB:
        async def fetch(self, sql, *args):
            if "SELECT * FROM aios.proposition" in sql:
                return [{"proposition_id":proposition,"canonical_text":"Window is open"}]
            if "CROSS JOIN LATERAL" in sql:
                return [{"proposition_id":proposition,"raw_text":"Open the window", "message_text":"Original message"}]
            return []
    result = asyncio.run(inspect_neighborhood(DB(), collection=SemanticIndexConfig().epistemic_collection,
                                              point_ids=[UUID(int=1), UUID(int=2)]))
    assert result["point_count"] == 2
    assert result["distinct_proposition_count"] == 1
    assert result["representatives"]["vector_count"] == 1
    assert len(result["members"][0]["points"]) == 2
    assert result["members"][0]["evidence"][0]["message_text"] == "Original message"


def test_cluster_keeps_sql_member_when_vector_is_missing(monkeypatch):
    from aios_app.semantic_index import inspection
    proposition = UUID(int=3)
    class Client:
        def retrieve(self, **kwargs): return []
        def close(self): pass
    monkeypatch.setattr(inspection,"QdrantStore",lambda *args: SimpleNamespace(client=Client()))
    class DB:
        async def fetchrow(self, sql, *args): return {"member_count":1}
        async def fetch(self, sql, *args):
            if "SELECT proposition_id, membership_kind" in sql:
                return [{"proposition_id":proposition,"membership_kind":"core"}]
            if "SELECT * FROM aios.proposition" in sql:
                return [{"proposition_id":proposition,"canonical_text":"Missing vector"}]
            return []
    result = asyncio.run(inspect_neighborhood(DB(),cluster_id=UUID(int=5)))
    assert result["missing_point_ids"] == [str(proposition)]
    assert len(result["members"]) == 1
    assert result["representatives"]["medoid_id"] is None


def test_asyncpg_json_fields_are_inspectable_objects():
    from aios_app.semantic_index.inspection import inspection_record
    row = inspection_record({"context":'{"viewpoint_id":"a"}',"frames":'[{"frame_id":"b"}]'})
    assert row["context"]["viewpoint_id"] == "a"
    assert row["frames"][0]["frame_id"] == "b"
