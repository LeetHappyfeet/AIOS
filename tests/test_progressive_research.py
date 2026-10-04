"""Progressive Research V1 pure contracts: identity, budgets and operation registration."""
from uuid import UUID

import pytest

from aios_app.topic_atlas.research_dossier import (
    MAX_SEARCH_RESULTS, MAX_SELECTED_PER_STEP, focus_key, _json_object,
)
from aios_app.agent.cognitive_operation_registry import CognitiveOperationRegistry


def test_focus_identity_deterministic_and_topic_scoped():
    assert focus_key("  Digital   ecology ") == focus_key("digital ecology")
    assert focus_key("Digital ecology", UUID("11111111-1111-1111-1111-111111111111")) != (
        focus_key("Digital ecology"))
    with pytest.raises(ValueError):
        focus_key("?")


def test_research_registry_reserves_research_faculty():
    registry = CognitiveOperationRegistry()
    for name in ("research.advance", "research.study"):
        operation = registry.prepare(
            operation_type=name,operation_payload={"query":"Digital World ecology"},
            faculty="research")
        assert operation.operation_type == name
        with pytest.raises(PermissionError):
            registry.prepare(operation_type=name,operation_payload={},faculty="reflection")


def test_research_receipt_is_json_compatible_on_retry():
    assert _json_object('{"status":"submitted_to_ingestion"}') == {
        "status":"submitted_to_ingestion"}
    assert _json_object({"status":"submitted"}) == {"status":"submitted"}
    assert _json_object("not-json") == {}
    assert MAX_SEARCH_RESULTS == 8
    assert MAX_SELECTED_PER_STEP == 2


def test_dossier_schema_keeps_submitted_distinct_from_semantic_admission():
    from pathlib import Path
    sql=(Path(__file__).parents[1]/"migrations"/"current"/
         "20261004_17_progressive_research.sql").read_text()
    assert "source_text_digest text NOT NULL" in sql
    assert "research_dedupe_key" in sql
    assert "aios.character_research_selection" in sql
    assert "aios.character_research_question" in sql
    assert "Integrity V4 approved" in sql  # explicitly states it is NOT


def test_fuseki_graph_navigation_returns_only_uuid_neighbors():
    from aios_app.topic_atlas.fuseki_navigation import query_neighbors, parse_neighbor_ids
    anchor=UUID("11111111-1111-1111-1111-111111111111")
    other=UUID("22222222-2222-2222-2222-222222222222")
    class StubFuseki:
        def query(self,dataset,sparql):
            assert dataset=="world"
            assert "urn:aios:topic-atlas:" in sparql
            assert f"urn:aios:knowledge-topic:{anchor}" in sparql
            assert "LIMIT 24" in sparql
            return {"results":{"bindings":[
                {"from":{"value":f"urn:aios:knowledge-topic:{anchor}"},
                 "to":{"value":f"urn:aios:knowledge-topic:{other}"}},
                {"from":{"value":f"urn:aios:knowledge-topic:{anchor}"},
                 "to":{"value":"https://untrusted.example/claim"}},
                {"from":{"value":f"urn:aios:knowledge-topic:{other}"},
                 "to":{"value":f"urn:aios:knowledge-topic:{anchor}"}},
            ]}}
    topic={"topic_id":anchor,"namespace":"catalog:fiction.digimon",
           "visibility":"catalog"}
    assert query_neighbors(StubFuseki(),topic)==(other,)
    assert parse_neighbor_ids({},anchor=anchor)==()


def test_consumption_reuses_acknowledged_dossier_receipt_and_rejects_stale_text():
    import asyncio
    import hashlib
    from aios_app.corpus import consume_corpus_sections
    instance=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    section=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    document=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    consumption=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    content="The source content that was examined."
    calls=[]
    class DB:
        async def fetchrow(self,sql,*args):
            if "FROM aios.character_instance" in sql:
                return {"character_id":"Renamon"}
            return {
                "section_id":section,"document_id":document,"content":content,
                "heading":"Heading","source_id":None,"document_kind":"document",
                "title":"Source","source_uri":None,"epistemic_namespace":"reference",
                "identity_binding":"external"}
        async def execute_returning_row(self,sql,*args):
            calls.append(args[-1])
            assert "ON CONFLICT (instance_id,research_dedupe_key)" in sql
            return {"consumption_id":consumption,"ingest_event_id":42,
                    "status":"ingested","section_id":section,"document_id":document,
                    "mode":"research"}
    digest=hashlib.md5(content.encode("utf-8")).hexdigest()
    async def run():
        for _ in range(2):
            result=await consume_corpus_sections(
                DB(),instance_id=instance,section_ids=[section],mode="research",
                dedupe_key_prefix="research-dossier:"+str(instance),
                expected_content_digests={section:digest})
            assert result["consumption_ids"]==[consumption]
        with pytest.raises(ValueError,match="changed"):
            await consume_corpus_sections(
                DB(),instance_id=instance,section_ids=[section],mode="research",
                dedupe_key_prefix="research-dossier:"+str(instance),
                expected_content_digests={section:"stale"})
    asyncio.run(run())
    assert len(calls)==2
    assert calls[0]==calls[1]==f"research-dossier:{instance}:{section}"
