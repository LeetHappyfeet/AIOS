"""Topic Atlas contracts: provisional subjects never become asserted world or character facts."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

from aios_app.topic_atlas.collector import normalize_label, topic_identity, topic_scope
from aios_app.topic_atlas.projection import (
    _topic_vector_text, topic_graph_location, topic_rdf_update,
    index_topics_once, project_topics_once,
)


def _topic(*, visibility="catalog", namespace="catalog:fiction.digimon"):
    return {
        "topic_id": UUID("11111111-1111-1111-1111-111111111111"),
        "namespace": namespace,
        "topic_kind": "entity",
        "display_label": "Renamon",
        "description": "",
        "status": "candidate",
        "visibility": visibility,
        "owner_character_id": "Renamon" if visibility == "private" else None,
        "vector_revision": 2,
        "graph_revision": 4,
    }


def test_topic_identity_is_stable_and_scope_partitioned():
    assert normalize_label("  ReNaMon   ") == "renamon"
    assert normalize_label("I") is None
    assert normalize_label("https://example.com") is None
    assert topic_identity("catalog:fiction.digimon","entity"," RENAMON ") == (
        topic_identity("catalog:fiction.digimon","entity","renamon"))
    assert topic_identity("char:Renamon","entity","renamon") != (
        topic_identity("catalog:fiction.digimon","entity","renamon"))
    assert topic_scope(character_id="Renamon", world_id="123") == (
        "char:Renamon","private","Renamon")
    assert topic_scope() == ("unresolved-source","private",None)


def test_graphs_are_separate_from_authoritative_projections_and_mark_advisory_edges():
    catalog = _topic()
    private = _topic(visibility="private",namespace="char:Renamon")
    assert topic_graph_location(catalog)[0] == "world"
    assert topic_graph_location(private)[0] == "char"
    for item in (catalog, private):
        dataset, graph, sparql, digest = topic_rdf_update(
            item,[{"normalized_alias":"fox digimon","display_alias":"Fox Digimon"}],
            [{"source_topic_id":item["topic_id"],
              "target_topic_id":UUID("22222222-2222-2222-2222-222222222222"),
              "relation_kind":"associated","status":"candidate"}])
        assert dataset in ("world","char")
        assert ":topic-atlas:" in graph
        assert "DELETE { GRAPH" in sparql and "INSERT { GRAPH" in sparql
        assert "hasNavigationRelation" in sparql and '"candidate"' in sparql
        assert "observesProposition" not in sparql and "hasBeliefState" not in sparql
        assert len(digest) == 64
    assert topic_graph_location(private)[1] != topic_graph_location(catalog)[1]


def test_topic_vector_is_label_and_alias_only_not_source_or_private_message():
    row = _topic()
    aliases = [{"display_alias":"Fox Digimon"}]
    assert _topic_vector_text(row,aliases) == (
        "topic: Renamon | type: entity | aliases: Fox Digimon")


def test_qdrant_receipt_only_after_upsert(monkeypatch):
    from aios_app.topic_atlas import projection
    sequence = []
    topic = _topic(visibility="private",namespace="char:Renamon")

    class DB:
        async def fetch(self, sql, *args):
            sequence.append("fetch")
            return [topic] if "SELECT t.*" in sql else []
        async def fetchrow(self, sql, *args):
            sequence.append("receipt")
            return {"topic_id":topic["topic_id"]}

    class Embedder:
        def embed(self, texts):
            sequence.append("embed")
            assert texts and "Renamon" in texts[0]
            return [[0.1, 0.2, 0.3] for _ in texts]

    class Store:
        def upsert(self, points):
            sequence.append("upsert")
            assert points[0].payload["visibility"] == "private"
            assert points[0].payload["owner_character_id"] == "Renamon"
            assert points[0].payload["topic_status"] == "candidate"

    monkeypatch.setattr(projection,"_get_embedder", lambda cfg: Embedder())
    monkeypatch.setattr(projection,"_get_store", lambda cfg, col: Store())
    cfg = SimpleNamespace(embedding_model="mock",embedding_version="v1",
                          topic_collection="knowledge_topics_v1")
    assert asyncio.run(index_topics_once(DB(),cfg)) == 1
    assert sequence.index("upsert") < sequence.index("receipt")


def test_fuseki_receipt_only_after_replacement(monkeypatch):
    sequence = []
    topic = _topic()
    class DB:
        async def fetch(self,sql,*args):
            if "SELECT t.*" in sql: return [topic]
            if "knowledge_topic_alias" in sql: return []
            if "knowledge_topic_relation" in sql: return []
            raise AssertionError(sql)
        async def fetchrow(self,sql,*args):
            sequence.append("receipt")
            return {"topic_id":topic["topic_id"]}
    class Fuseki:
        def update(self,dataset,sparql):
            sequence.append("update")
            assert dataset == "world"
            assert ":topic-atlas:" in sparql
    assert asyncio.run(project_topics_once(DB(),Fuseki())) == 1
    assert sequence == ["update","receipt"]


def test_failed_fuseki_replacement_must_not_create_a_receipt():
    topic = _topic()
    class DB:
        async def fetch(self,sql,*args):
            if "SELECT t.*" in sql: return [topic]
            return []
        async def fetchrow(self,sql,*args):
            raise AssertionError("receipt must not be written")
    class BrokenFuseki:
        def update(self,*args):
            raise RuntimeError("offline")
    import pytest
    with pytest.raises(RuntimeError,match="offline"):
        asyncio.run(project_topics_once(DB(),BrokenFuseki()))
