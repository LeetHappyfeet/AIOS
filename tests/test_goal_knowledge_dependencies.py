"""Goal Integration V1: authority boundaries, coverage and dispatch contracts."""
import asyncio
from uuid import uuid4

from aios_app.agent.cognitive_subjects import (
    CognitiveSubject, GoalKnowledgeDemandResolver,
)
from aios_app.agent.opportunities import goal_research_operation
from aios_app.hud.render_text import render_hud_text


def _subject():
    return CognitiveSubject(
        subject_id=uuid4(), canonical_key="goal:test", subject_type="goal_knowledge",
        entity_keys=(), predicate_key=None, object_key=None, topic_key=None,
        question_type="knowledge_demand",
        question="Find evidence about gendered sports media coverage statistics",
        display_label="Gendered sports media coverage statistics",
        confidence=1.0, uncertainty=0.0, salience=0.9,
    )


def _evidence(*, proposition=None, source=None, **extra):
    return {
        "claim_kind": "BELIEF", "proposition_id": proposition or uuid4(),
        "source_node_id": source or uuid4(),
        "text": "Gendered sports media coverage statistics",
        **extra,
    }


def test_goal_coverage_deduplicates_graph_representations_and_provisional_text():
    subject=_subject()
    original=_evidence()
    duplicates=[
        original,
        {**original, "topology_node_id": uuid4(), "node_type": "TOPIC"},
        {**original, "topology_node_id": uuid4(), "node_type": "SEMANTIC_PIVOT"},
        _evidence(claim_kind="GOAL"),
        _evidence(authority_state="candidate"),
        _evidence(cognitive_provisional=True),
        {"claim_kind":"BELIEF","proposition_id":"cognitive:synthetic",
         "text":"Gendered sports media coverage statistics"},
    ]
    result=GoalKnowledgeDemandResolver.established_support(subject,duplicates)
    assert result["independent_support_count"]==1
    assert result["coverage_status"]=="partial"
    assert len(result["evidence_ids"])==1


def test_independent_established_source_receipts_can_make_coverage_sufficient():
    subject=_subject()
    rows=[_evidence() for _ in range(3)]
    result=GoalKnowledgeDemandResolver.established_support(subject,rows)
    assert result["independent_support_count"]==3
    assert result["coverage_status"]=="sufficient"
    assert result["term_coverage"]==1.0
    assert GoalKnowledgeDemandResolver.established_support(subject,[])["coverage_status"]=="missing"


def test_three_nodes_from_one_source_do_not_claim_independent_sufficiency():
    origin=uuid4()
    rows=[_evidence(source=origin) for _ in range(3)]
    assert GoalKnowledgeDemandResolver.established_support(_subject(),rows)["coverage_status"]=="partial"


def test_topology_timeout_is_unavailable_not_missing_and_not_research_permission():
    class TimedOut:
        async def fetch(self,*args,**kwargs):
            raise TimeoutError("contended topology query")
    resolver=GoalKnowledgeDemandResolver(TimedOut())
    result=asyncio.run(resolver.resolve(
        instance_id=uuid4(),subject=_subject(),known=[]))
    assert result["coverage_status"]=="unavailable"
    assert result["retrieval_state"]=="unavailable"
    assert result["next_source"]=="defer"
    assert result["evidence_ids"]==[]


def test_declared_retrieval_budget_miss_skips_secondary_graph_query():
    class MustNotCall:
        async def fetch(self,*args,**kwargs):
            raise AssertionError("HUD already proved topology unavailable")
    result=asyncio.run(GoalKnowledgeDemandResolver(MustNotCall()).resolve(
        instance_id=uuid4(),subject=_subject(),known=[],
        retrieval_unavailable=True))
    assert result["coverage_status"]=="unavailable"
    assert result["next_source"]=="defer"


def test_goal_progression_inquiry_then_dossier_only_for_verified_gap():
    assert goal_research_operation("memory",None)=="inquiry.resolve"
    assert goal_research_operation("memory","unresolved")=="research.advance"
    assert goal_research_operation("memory","partial")=="research.advance"
    assert goal_research_operation("memory","resolved",coverage_status="partial")=="research.advance"
    assert goal_research_operation("corpus","unresolved","no_access") is None
    assert goal_research_operation("corpus","unresolved","no_access",
                                   access_changed=True)=="research.advance"
    assert goal_research_operation("memory","planning") is None
    assert goal_research_operation("memory","unresolved",coverage_status="sufficient") is None
    assert goal_research_operation("defer","unresolved",coverage_status="unavailable",
                                   retrieval_state="unavailable") is None
    assert goal_research_operation("memory",None,retrieval_state="unavailable") is None


def test_goal_hud_displays_requirement_not_false_progress():
    goal=uuid4()
    frame={
        "identity":{"name":"Renamon"},"presence":{"instance_id":str(uuid4())},
        "scene":{"working_state":{"immediate_goal":{
            "goal_id":str(goal),"text":"Research sports-media sources",
            "knowledge_requirements":[{
                "coverage_status":"missing", "dossier_ids":[str(uuid4())],
            }],
        }}},
        "goals":[{"goal_id":str(goal),"text":"Research sports-media sources"}],
    }
    hud=render_hud_text(frame)
    assert "Goal knowledge: missing; linked research dossiers: 1" in hud
    assert "completed" not in hud.lower()



def test_idle_dossier_reopens_only_on_new_index_or_scoped_acl_epoch():
    from datetime import datetime, timedelta, timezone
    from aios_app.topic_atlas.research_dossier import ProgressiveResearchService

    finished=datetime.now(timezone.utc)-timedelta(minutes=10)
    class EpochDB:
        def __init__(self,epoch):
            self.epoch=epoch
            self.queued=0
            self.inserts=0
            self.queries=[]
        async def fetchrow(self,sql,*args):
            self.queries.append(sql)
            if "INSERT INTO aios.character_research_question" in sql:
                self.inserts+=1
                self.queued=1
                return {"question_id":uuid4()}
            if "FROM aios.character_research_dossier d" in sql:
                return {
                    "dossier_id":uuid4(), "question":"Women's sports media coverage",
                    "status":"open", "max_cycles":3, "max_sections":8,
                    "attempts":1, "last_completed_at":finished,
                    "sources":0, "queued":self.queued, "expired":0,
                }
            raise AssertionError(sql)
        async def fetchval(self,sql,*args):
            self.queries.append(sql)
            assert "character_corpus_access" in sql
            assert "character_knowledge_domain" in sql
            assert "corpus_discovery_projection" in sql
            return self.epoch

    before=EpochDB(finished-timedelta(minutes=1))
    assert asyncio.run(ProgressiveResearchService(before).available(
        instance_id=uuid4(),question="Women's sports media coverage")) is False
    assert before.inserts==0

    new_reference=EpochDB(finished+timedelta(minutes=1))
    svc=ProgressiveResearchService(new_reference)
    instance=uuid4()
    assert asyncio.run(svc.available(
        instance_id=instance,question="Women's sports media coverage")) is True
    assert new_reference.inserts==1
    assert asyncio.run(svc.available(
        instance_id=instance,question="Women's sports media coverage")) is True
    assert new_reference.inserts==1  # Queued once, no repeated insert per cycle.


def test_requirement_schema_never_equates_research_with_goal_completion():
    from pathlib import Path
    sql=(Path(__file__).parents[1]/"migrations"/"current"/
         "20261004_20_goal_knowledge_dependencies.sql").read_text()
    assert "UNIQUE (instance_id,goal_id,requirement_key)" in sql
    assert "FOREIGN KEY (requirement_id,goal_id,instance_id)" in sql
    assert "FOREIGN KEY (goal_id,dossier_id)" in sql
    assert "character_goal_evidence" not in sql
    assert "semantic_integrity" not in sql
