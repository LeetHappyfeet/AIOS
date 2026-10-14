"""Knowledge Atlas stabilization unit/regression contracts."""
import asyncio
from uuid import UUID

import pytest

from aios_app.topic_atlas.collector import normalize_label
from aios_app.agent.research_action import extract_research_request,strip_tool_markup
from aios_app.hud.render_text import render_hud_text
from aios_app.topic_atlas import projection


def test_topic_filter_rejects_internal_id_not_legitimate_subject():
    assert normalize_label("1c27907d0c0a96e6862ab0ff74a7e14f3f15aa5eb8c0d01dc3164931845b1903") is None
    assert normalize_label("6da929cb577e829493cf755ec1f31f3e2dd452179bce8f9c380e4f41dbefca74") is None
    assert normalize_label("73d24e0c-0a96-e686-2ab0-ff74a7e14f3f") is None
    assert normalize_label("seventeen and") is None
    assert normalize_label("Renamon") == "renamon"
    assert normalize_label("women's sports media coverage") == "women's sports media coverage"
    assert normalize_label("glass") == "glass"


def test_source_action_parser_preserves_narrative_and_rejects_ambiguity():
    msg=('<aios_action>{"op":"research","question":"Citable studies of women in sports"}</aios_action>'
         '\n*Renamon moved the phone across the table.*')
    assert extract_research_request(msg) == "Citable studies of women in sports"
    assert strip_tool_markup(msg) == "*Renamon moved the phone across the table.*"
    assert extract_research_request("Only dialogue") is None
    with pytest.raises(ValueError,match="one research"):
        extract_research_request(msg+msg)
    with pytest.raises(ValueError,match="malformed"):
        extract_research_request("<aios_action>{broken</aios_action>")


def test_hud_does_not_replay_legacy_source_text_as_scene_change():
    msg='<aios_action>{"op":"research","question":"Sport coverage"}</aios_action> Scene text'*20
    frame={
        "identity":{"name":"Renamon"},
        "presence":{"instance_id":"example"},
        "scene":{"working_state":{
            "immediate_goal":{"text":"Renamon intends to answer.","goal_id":"g1"},
            "last_significant_change":{"text":msg,"source":"dag_node"},
        }},
        "goals":[
            {"text":"Renamon intends to answer.","goal_id":"g1"},
            {"text":"Renamon intends to remain silent.","goal_id":"g2"},
        ],
        "research_activity":{"question":"Citable studies of women's sports coverage",
                             "status":"completed","outcome":"no_results",
                             "source_count":0,"durable_knowledge":False},
    }
    rendered=render_hud_text(frame)
    assert "Immediate managed goal:" in rendered
    assert "OTHER ACTIVE GOALS:" in rendered
    assert "Renamon intends to remain silent" in rendered
    assert "RESEARCH ACTIVITY:" in rendered
    assert "Authorized references found: 0 (not acquired knowledge)" in rendered
    assert "<aios_action>" not in rendered
    assert "Last scene change: " not in rendered


def test_topic_tombstone_never_acknowledges_before_qdrant_delete(monkeypatch):
    topic=UUID("11111111-1111-1111-1111-111111111111")
    events=[]
    class Client:
        def delete(self,**kwargs):
            events.append(("qdrant",kwargs))
    class Store:
        client=Client()
    class DB:
        async def fetch(self,sql,*args):
            assert "t.status='retired'" in sql
            return [{"topic_id":topic,"vector_revision":3,
                     "vector_collection":"knowledge_topics_v1"}]
        async def fetchrow(self,sql,*args):
            events.append(("receipt",args))
            return {"topic_id":topic}
    monkeypatch.setattr(projection,"_get_store",lambda cfg,collection: Store())
    result=asyncio.run(projection.prune_retired_topic_vectors_once(DB(),object(),limit=4))
    assert result == 1
    assert [kind for kind,_ in events] == ["qdrant","receipt"]
    assert events[0][1]["wait"] is True


def test_retired_topic_graph_cleared_before_receipt():
    topic=UUID("22222222-2222-2222-2222-222222222222")
    events=[]
    class Fuseki:
        def update(self,dataset,sparql):
            assert dataset=="world"
            assert sparql.startswith("CLEAR SILENT GRAPH")
            events.append("clear")
    class DB:
        async def fetch(self,sql,*args):
            return [{"topic_id":topic,"namespace":"catalog:digimon","visibility":"catalog",
                     "status":"retired","graph_revision":5}]
        async def fetchrow(self,sql,*args):
            events.append("receipt")
            return {"topic_id":topic}
    assert asyncio.run(projection.project_topics_once(DB(),Fuseki(),limit=1)) == 1
    assert events == ["clear","receipt"]
