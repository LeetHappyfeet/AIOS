from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.hud.context import HUDContextResolver
from aios_app.epistemic.cognitive_context import CognitiveContextService
from aios_app.epistemic.relevance import CognitiveRelevanceScorer
from aios_app.epistemic.research import KnowledgeDemandResolver
from aios_app.agent.cognitive_subjects import (\n    CognitiveSubjectBuilder, SubjectKnowledgeDemandResolver,\n    GoalSubjectProjector, GoalKnowledgeDemandResolver,\n)

_WORDS=re.compile(r"[A-Za-z0-9][A-Za-z0-9_' -]{1,80}")


@dataclass(frozen=True)
class OpportunityBatch:
    instance_id: UUID
    opportunities: tuple[dict[str,Any], ...]


def _clip(v:Any,n:int=180)->str:
    return " ".join(str(v or "").split())[:n].rstrip()


class CognitiveOpportunityService:
    """Project existing cognition/graphs into small evidence-backed possible thoughts."""

    def __init__(self, db:Database):
        self.db=db
        self.contexts=HUDContextResolver(db)
        self.cognition=CognitiveContextService(db)
        self.demand=KnowledgeDemandResolver(minimum_terms=1,coverage_threshold=.60)
        self.subjects=CognitiveSubjectBuilder(db)
        self.subject_demand=SubjectKnowledgeDemandResolver()\n        self.goal_subjects=GoalSubjectProjector(db)\n        self.goal_demand=GoalKnowledgeDemandResolver(db)

    async def generate(self, *, instance_id:UUID, source_node_id:UUID|None=None,
                       limit:int=8) -> OpportunityBatch:
        context=await self.contexts.resolve(instance_id)
        raw=await self.db.fetchrow(
            "SELECT * FROM aios.character_runtime_state WHERE instance_id=$1",instance_id)
        if not raw: return OpportunityBatch(instance_id,())
        raw=dict(raw)
        delta_focus=None
        if source_node_id is not None:
            source_row=await self.db.fetchrow(
                "SELECT message_text FROM aios.dag_node WHERE node_id=$1 AND timeline_id=$2",
                source_node_id,context.source_timeline_id)
            if source_row and source_row["message_text"]:
                delta_focus=str(source_row["message_text"])
        attention=await self.cognition.resolve_attention_inputs(
            context,raw,{},recent_limit=6,focus_text=delta_focus)
        snapshot=await self.cognition.resolve_knowledge(context,None,attention)
        focus=_clip(attention.focus_text,360)
        goals=list(attention.goals)
        goal_states=await self.cognition.goals.cognitive_states(instance_id, goals)
        proposals:list[dict[str,Any]]=[]
        subjects=await self.subjects.build(
            instance_id=instance_id,focus_text=focus,knowledge=list(snapshot.knowledge),
            source_node_id=source_node_id or context.source_head_node_id)
        primary_subject=subjects[0] if subjects else None
        structured_demand=(
            self.subject_demand.resolve(primary_subject,list(snapshot.knowledge))
            if primary_subject else None
        )

        # Established topology recall is already character-relative and ranked.
        for rank,item in enumerate(snapshot.recalled_memories[:3]):
            text=_clip(item.get("text"),180)
            if not text: continue
            rel=float((item.get("relevance") or {}).get("recall")
                      or (item.get("relevance") or {}).get("total") or 0)
            proposals.append(self._p(
                "memory_recall",f"Recall what {text}", "memory.retrieve",
                {"proposition_id":str(item.get("proposition_id") or ""),"focus":focus},
                source_node_id or context.source_head_node_id,context,
                relevance=min(1,rel/5),memory_affinity=min(1,rel/4),
                recency=max(.2,1-rank*.25),
                evidence=[{"kind":"recalled_memory","id":str(item.get("proposition_id") or "")}],
                key=f"memory:{item.get('proposition_id') or text[:80]}",
                subject_id=primary_subject.subject_id if primary_subject else None))

        known=[str(x.get("text") or "") for x in snapshot.knowledge]
        kd=snapshot.corpus_demand
        if structured_demand and structured_demand["next_source"]=="corpus":
            subject=primary_subject.display_label
            query=structured_demand["query"]
            gap=max(0.0,1.0-float(structured_demand["internal_coverage"]))
            proposals.append(self._p(
                "knowledge_gap",f"Find out more about {subject}.","corpus.search",
                {"query":query,"focus":primary_subject.retrieval_text,
                 "subject_id":str(primary_subject.subject_id)},
                source_node_id or context.source_head_node_id,context,
                relevance=.7,knowledge_gap=gap,novelty=.7,recency=1,
                evidence=[{"kind":"subject_knowledge_demand",
                           "internal_coverage":structured_demand["internal_coverage"]}],
                key=f"research:{primary_subject.canonical_key}"[:180],
                subject_id=primary_subject.subject_id))
        elif not primary_subject:
            if not kd and focus:
                d=self.demand.resolve(focus,known_texts=known)
                if d.needed:
                    kd={"missing_terms":list(d.missing_terms),"coverage":d.coverage,"reason":d.reason}
            if kd:
                missing=list(kd.get("missing_terms") or [])
                subject=" ".join(missing[:6]) or focus
                if subject:
                    gap=max(0,min(1,1-float(kd.get("coverage") or 0)))
                    proposals.append(self._p(
                        "knowledge_gap",f"Find out more about {subject}.","corpus.search",
                        {"query":subject,"focus":focus},source_node_id or context.source_head_node_id,
                        context,relevance=.65,knowledge_gap=gap,novelty=.7,recency=1,
                        evidence=[{"kind":"corpus_demand","reason":kd.get("reason")}],
                        key=f"research:{subject.lower()[:100]}"))

        goal_words=set(re.findall(r"[a-z0-9']+",focus.lower()))
        for goal in goals[:3]:
            g=_clip(goal.text,160)
            overlap=len(goal_words & set(re.findall(r"[a-z0-9']+",g.lower())))
            affinity=min(1,.25*overlap)
            if affinity>.0 or len(goals)==1:
                goal_id=str(goal.goal_id) if goal.goal_id else None
                goal_subject_entry=goal_subject_demands.get(goal.goal_id) if goal.goal_id else None
                goal_subject=goal_subject_entry[0] if goal_subject_entry else None
                goal_demand=goal_subject_entry[1] if goal_subject_entry else None
                proposals.append(self._p(
                    "goal_review",f"Consider whether what just happened changes my goal: {g}",
                    "planning.review",{"goal_id":goal_id,"goal":g,"focus":focus,
                    "goal_state":goal_states.get(goal.goal_id,{}) if goal.goal_id else {},
                    "knowledge_demand":goal_demand or {}},
                    source_node_id or context.source_head_node_id,context,
                    relevance=.45+affinity*.35,goal_affinity=max(.35,affinity),recency=1,
                    evidence=[{"kind":"active_goal","goal_id":goal_id,"text":g}],
                    key=f"goal:{goal_id or g.lower()[:100]}",
                    subject_id=goal_subject.subject_id if goal_subject else
                               (primary_subject.subject_id if primary_subject else None)))

        # Scene transitions are explicit deterministic evidence for immediate/reflection needs.
        if context.source_head_node_id:
            rows=await self.db.fetch(
                """SELECT slot_key,before_value,after_value,source_node_id
                   FROM aios.character_scene_transition
                   WHERE instance_id=$1 AND source_node_id=$2
                   ORDER BY created_at DESC LIMIT 4""",
                instance_id,context.source_head_node_id)
            for row in rows:
                slot=str(row["slot_key"])
                after=_clip(row["after_value"],120)
                if not after: continue
                immediate=slot in {"pending_action","immediate_goal","location"}
                proposals.append(self._p(
                    "immediate" if immediate else "reflection",
                    (f"Decide what to do about {after}." if immediate
                     else f"Think about what this change means to me: {after}."),
                    "executive.review" if immediate else "reflection.review",
                    {"slot":slot,"value":after,"focus":focus},
                    row["source_node_id"],context,relevance=.7,urgency=.9 if immediate else .2,
                    novelty=.7,recency=1,
                    freshness="strict" if immediate else "contextual",
                    evidence=[{"kind":"scene_transition","slot":slot}],
                    key=f"scene:{slot}:{after.lower()[:80]}",
                    subject_id=primary_subject.subject_id if primary_subject else None))

        proposals.sort(key=lambda x:x["priority_score"],reverse=True)
        stored=[]
        for p in proposals[:max(1,min(limit,16))]:
            row=await self.db.execute_returning_row(
                """INSERT INTO aios.character_cognitive_opportunity(
                   instance_id,opportunity_type,natural_language,operation_type,operation_payload,
                   source_node_id,source_timeline_id,source_state_version,novelty,relevance,urgency,
                   uncertainty,goal_affinity,memory_affinity,knowledge_gap,recency,priority_score,
                   freshness_policy,supersession_key,evidence,valid_until,subject_id)
                   VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20::jsonb,
                          now()+CASE WHEN $18='strict' THEN interval '2 minutes' ELSE interval '15 minutes' END,$21)
                   ON CONFLICT (instance_id,supersession_key) WHERE status IN ('pending','offered')
                   DO UPDATE SET natural_language=EXCLUDED.natural_language,
                     operation_payload=EXCLUDED.operation_payload,source_node_id=EXCLUDED.source_node_id,
                     source_state_version=EXCLUDED.source_state_version,priority_score=EXCLUDED.priority_score,
                     evidence=EXCLUDED.evidence,valid_until=EXCLUDED.valid_until,
                     subject_id=COALESCE(EXCLUDED.subject_id,aios.character_cognitive_opportunity.subject_id),
                     updated_at=now()
                   RETURNING *""",
                instance_id,p["opportunity_type"],p["natural_language"],p["operation_type"],
                json.dumps(p["operation_payload"]),p["source_node_id"],p["source_timeline_id"],
                p["source_state_version"],p["novelty"],p["relevance"],p["urgency"],p["uncertainty"],
                p["goal_affinity"],p["memory_affinity"],p["knowledge_gap"],p["recency"],
                p["priority_score"],p["freshness_policy"],p["supersession_key"],
                json.dumps(p["evidence"]),p.get("subject_id"))
            if row: stored.append(dict(row))
        return OpportunityBatch(instance_id,tuple(stored))

    def _p(self,typ,label,op,payload,node,context,*,novelty=0,relevance=0,urgency=0,
           uncertainty=0,goal_affinity=0,memory_affinity=0,knowledge_gap=0,recency=0,
           evidence=(),key:str,freshness="contextual",subject_id:UUID|None=None):
        score=(1.7*urgency+1.35*goal_affinity+1.2*relevance+1.1*knowledge_gap+
               .9*memory_affinity+.65*novelty+.45*uncertainty+.55*recency)
        return {"opportunity_type":typ,"natural_language":_clip(label,220),"operation_type":op,
                "operation_payload":payload,"source_node_id":node,
                "source_timeline_id":context.source_timeline_id,
                "source_state_version":context.state_version,"novelty":novelty,
                "relevance":relevance,"urgency":urgency,"uncertainty":uncertainty,
                "goal_affinity":goal_affinity,"memory_affinity":memory_affinity,
                "knowledge_gap":knowledge_gap,"recency":recency,"priority_score":score,
                "freshness_policy":freshness,"supersession_key":key,"evidence":list(evidence),
                "subject_id":subject_id}
