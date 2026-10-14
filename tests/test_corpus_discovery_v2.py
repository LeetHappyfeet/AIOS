"""Corpus Discovery V2: discovery-only vectors, projection receipts and RPC contracts."""
import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from aios_app.semantic_index import corpus_discovery
from aios_app.semantic_index.query_local import LocalSemanticQueryService

SECTION_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DOC_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TOPIC_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def source():
    return {
        "section_id": SECTION_ID,
        "document_id": DOC_ID,
        "heading":"Digital ecology",
        "title":"Renamon field notes",
        "author":"Reference author",
        "facets":["subject: Digimon"],
        "domains":["fiction.digimon"],
        "content":"A wooded ecosystem and other source material",
        "source_fingerprint":"abc123",
    }


def config():
    return SimpleNamespace(corpus_collection="corpus_sections_v2",
        topic_collection="knowledge_topics_v1",embedding_model="mock",
        embedding_version="v1")


def test_corpus_embedding_includes_metadata_without_claiming_truth():
    text = corpus_discovery.corpus_embedding_text(source())
    assert "Renamon field notes" in text
    assert "Digital ecology" in text
    assert "fiction.digimon" in text
    assert "subject: Digimon" in text
    assert "wooded ecosystem" in text


def test_source_revision_receipt_after_successful_upsert(monkeypatch):
    sequence = []
    class FakeDB:
        async def fetch(self,sql,*args):
            assert "source_fingerprint" in sql
            return [source()]
        async def fetchrow(self,sql,*args):
            sequence.append("receipt")
            assert "source_fingerprint=$2" in sql
            return {"section_id":SECTION_ID}

    class Embedder:
        def embed(self,texts):
            sequence.append("embed")
            return [[0.1,0.2,0.3] for _ in texts]
    class Store:
        def upsert(self,points):
            sequence.append("upsert")
            payload=points[0].payload
            assert payload["section_id"]==str(SECTION_ID)
            assert "title" not in payload and "content" not in payload
            assert payload["source_fingerprint"]=="abc123"

    monkeypatch.setattr(corpus_discovery,"_get_embedder",lambda cfg:Embedder())
    monkeypatch.setattr(corpus_discovery,"_get_store",lambda cfg,col:Store())
    assert asyncio.run(corpus_discovery.index_corpus_discovery_once(FakeDB(),config()))==1
    assert sequence == ["embed","upsert","receipt"]


def test_failed_upsert_never_creates_vector_receipt(monkeypatch):
    class FakeDB:
        async def fetch(self,sql,*args): return [source()]
        async def fetchrow(self,*args): raise AssertionError("receipt must not be reached")
    class Embedder:
        def embed(self,texts): return [[0.1,0.2,0.3]]
    class Store:
        def upsert(self,points): raise RuntimeError("Qdrant offline")
    monkeypatch.setattr(corpus_discovery,"_get_embedder",lambda cfg:Embedder())
    monkeypatch.setattr(corpus_discovery,"_get_store",lambda cfg,col:Store())
    with pytest.raises(RuntimeError,match="offline"):
        asyncio.run(corpus_discovery.index_corpus_discovery_once(FakeDB(),config()))


def test_query_embeds_once_and_never_exposes_cold_corpus_payload(monkeypatch):
    import aios_app.semantic_index.query_local as local
    calls=[]
    class Embedder:
        def embed(self,texts):
            calls.append(("embed",tuple(texts)))
            return [[0.1,0.2]]
    class Store:
        def __init__(self,name): self.name=name
        def search(self,vector,*,top_k,qdrant_filter):
            calls.append((self.name,top_k))
            if self.name=="corpus_sections_v2":
                return [(str(SECTION_ID),0.89,{"section_id":str(SECTION_ID),
                          "title":"PRIVATE SOURCE TITLE","content":"PRIVATE CONTENT"})]
            return [(str(TOPIC_ID),0.8,{"topic_id":str(TOPIC_ID),
                        "display_label":"PRIVATE OTHER TOPIC"})]
    monkeypatch.setattr(local,"_get_embedder",lambda cfg:Embedder())
    monkeypatch.setattr(local,"_get_store",lambda cfg,name:Store(name))
    result=LocalSemanticQueryService(config()).search_corpus_discovery("Renamon")
    assert len([c for c in calls if c[0]=="embed"])==1
    assert result==[
        ("corpus",0.89,{"section_id":str(SECTION_ID)}),
        ("topic",0.8,{"topic_id":str(TOPIC_ID)}),
    ]
    assert all("PRIVATE" not in str(hit) for hit in result)


def test_no_candidate_source_is_ever_admitted_as_character_knowledge():
    from aios_app.epistemic.research import CorpusResearchHit,CorpusResearchResult
    hit=CorpusResearchHit(SECTION_ID,DOC_ID,0.9,"Reference","Heading",
                          "Unexamined passage",("fiction.digimon",),("semantic","topic"))
    result=CorpusResearchResult(TOPIC_ID,TOPIC_ID,"Renamon","ecology",("ecology",),
                                (hit,),"searched")
    [reference]=result.reference_context()
    assert reference["durable_knowledge"] is False
    assert reference["retrieval_methods"]==["semantic","topic"]
    assert reference["kind"]=="corpus_reference"
