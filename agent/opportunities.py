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
from aios_app.agent.cognitive_operation_registry import CognitiveOperationRegistry
from aios_app.agent.cognitive_subjects import (
    CognitiveSubjectBuilder, SubjectKnowledgeDemandResolver,
    GoalSubjectProjector, GoalKnowledgeDemandResolver,
)

_WORDS=re.compile(r"[A-Za-z0-9][A-Za-z0-9_' -]{1,80}")


def _json_default(value: Any) -> Any:
    """Normalize typed identifiers at the JSON/JSONB persistence boundary."""
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


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
        self.subject_demand=SubjectKnowledgeDemandResolver()
        self.goal_subjects=GoalSubjectProjector(db)
        self.goal_demand=GoalKnowledgeDemandResolver(db)
        self.cognitive_operations=CognitiveOperationRegistry()

    async def generate(self, *, instance_id:UUID, source_node_id:UUID|None=None,
                       limit:int=8, focus_override:str|None=None) -> OpportunityBatch:
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
            context,raw,{},recent_limit=6,focus_text=focus_override or delta_focus)
        snapshot=await self.cognition.resolve_knowledge(context,None,attention)
        scene_row=await self.db.fetchrow(
            """SELECT scene_state FROM aios.character_scene_snapshot
               WHERE instance_id=$1
                 AND source_timeline_id IS NOT DISTINCT FROM $2
                 AND source_head_node_id IS NOT DISTINCT FROM $3
               ORDER BY updated_at DESC LIMIT 1""",
            instance_id,context.source_timeline_id,context.source_head_node_id)
        current_scene=self._json_object(scene_row["scene_state"]) if scene_row else {}
        focus=_clip(attention.focus_text,360)
        goals=list(attention.goals)
        current_scene["immediate_goal"] = goals[0].hud_item() if goals else None
        goal_states=await self.cognition.goals.cognitive_states(instance_id, goals)
        proposals:list[dict[str,Any]]=[]
        # The character can elect to consider a new intention. Formation is
        # limited to one attempt per source node and the planning model must
        # ground its proposal in that node before the goal service admits it.
        if context.source_head_node_id and focus and len(focus.split()) >= 5 and len(goals)<3:
            attempted=await self.db.fetchval(
                """SELECT 1 FROM aios.character_cognitive_operation
                   WHERE instance_id=$1 AND source_node_id=$2
                     AND operation_type='planning.form_goal' LIMIT 1""",
                instance_id,context.source_head_node_id)
            if not attempted:
                proposals.append(self._p(
                    "goal_formation","Consider whether this event calls for a new objective.",
                    "planning.form_goal",{"focus":focus},
                    context.source_head_node_id,context,
                    relevance=.55,novelty=.65,recency=1,
                    evidence=[{"kind":"source_node","id":str(context.source_head_node_id)}],
                    key=f"goal-formation:{context.source_head_node_id}",freshness="strict"))
        subjects=await self.subjects.build(
            instance_id=instance_id,focus_text=focus,knowledge=list(snapshot.knowledge),
            source_node_id=source_node_id or context.source_head_node_id)
        primary_subject=subjects[0] if subjects else None
        structured_demand=(
            self.subject_demand.resolve(primary_subject,list(snapshot.knowledge))
            if primary_subject else None
        )
        goal_subject_demands:dict[UUID,tuple[Any,dict[str,Any]]]={}
        for goal in goals:
            if not goal.goal_id:
                continue
            goal_subject=await self.goal_subjects.project(instance_id=instance_id,goal=goal)
            if goal_subject is None:
                continue
            demand=await self.goal_demand.resolve(
                instance_id=instance_id,subject=goal_subject,known=list(snapshot.knowledge))
            goal_subject_demands[goal.goal_id]=(goal_subject,demand)
            if demand["next_source"]=="corpus":
                gap=max(0.0,1.0-float(demand["internal_coverage"]))
                proposals.append(self._p(
                    "knowledge_gap",f"Find knowledge needed for my goal: {goal.text}",
                    "inquiry.resolve",
                    {"query":demand["query"],"allow_model":gap >= .75,
                     "focus":goal_subject.retrieval_text,
                     "subject_id":str(goal_subject.subject_id),"goal_id":str(goal.goal_id)},
                    source_node_id or context.source_head_node_id,context,
                    relevance=.72,goal_affinity=.85,knowledge_gap=gap,novelty=.65,recency=1,
                    evidence=[{"kind":"goal_knowledge_demand","goal_id":str(goal.goal_id),
                               "internal_coverage":demand["internal_coverage"],
                               "coverage_source":demand["coverage_source"]}],
                    key=f"goal-research:{goal.goal_id}",
                    subject_id=goal_subject.subject_id))

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
                "knowledge_gap",f"Investigate {subject} progressively.","research.advance",
                {"query":query,"allow_model":gap >= .75,
                 "focus":primary_subject.retrieval_text,
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
                        "knowledge_gap",f"Investigate {subject} progressively.","research.advance",
                        {"query":subject,"focus":focus,"allow_model":False,source_node_id or context.source_head_node_id,
                        context,relevance=.65,knowledge_gap=gap,novelty=.7,recency=1,
                        evidence=[{"kind":"corpus_demand","reason":kd.get("reason")}],
                        key=f"research:{subject.lower()[:100]}"))

        goal_words=set(re.findall(r"[a-z0-9']+",focus.lower()))
        # Review is budgeted, not eligibility-limited. Rank every active goal
        # by current affinity plus review starvation, then spend at most three
        # review slots. This prevents goals 4-N from becoming immortal merely
        # because older goals sort ahead of them.
        review_rows=await self.db.fetch(
            """SELECT g.goal_id,
                      max(o.completed_at) FILTER (
                        WHERE o.operation_type='planning.review'
                          AND o.status='succeeded'
                      ) AS last_reviewed_at
               FROM aios.character_agent_goal g
               LEFT JOIN aios.character_cognitive_operation o
                 ON o.instance_id=g.instance_id
                AND o.input->>'goal_id'=g.goal_id::text
               WHERE g.instance_id=$1 AND g.status='active'
               GROUP BY g.goal_id""",
            instance_id)
        last_reviewed={row["goal_id"]:row["last_reviewed_at"] for row in review_rows}
        # A bounded, already scoped source-turn window lets planning.review
        # inspect the character's preceding answer and the participant's
        # acknowledgment, not just the current focus string.
        recent_source_events=[
            {"node_id":str(row.get("node_id") or ""),
             "speaker_id":str(row.get("speaker_id") or ""),
             "message_text":_clip(row.get("message_text") or "",300)}
            for row in attention.recent_newest
            if row.get("event_stream")=="source" and row.get("message_text")
        ][:4]
        review_candidates=[]
        for index,goal in enumerate(goals):
            g=_clip(goal.text,160)
            overlap=len(goal_words & set(re.findall(r"[a-z0-9']+",g.lower())))
            affinity=min(1,.25*overlap)
            reviewed=last_reviewed.get(goal.goal_id)
            # Never-reviewed goals outrank already-reviewed zero-affinity goals;
            # otherwise older reviews rotate forward deterministically.
            starvation=1.0 if reviewed is None else 0.0
            completion_due=bool(goal_states.get(goal.goal_id,{}).get("completion_candidate_count"))
            relevant=overlap>0
            review_candidates.append((completion_due,relevant,starvation,affinity,reviewed,index,goal,g))
        review_candidates.sort(
            key=lambda x:(-int(x[0]),-int(x[1]),-x[2],-x[3],
                          x[4] is not None,x[4],x[5]))
        for completion_due,relevant,starvation,affinity,reviewed,index,goal,g in review_candidates[:3]:
            goal_id=str(goal.goal_id) if goal.goal_id else None
            goal_subject_entry=goal_subject_demands.get(goal.goal_id) if goal.goal_id else None
            goal_subject=goal_subject_entry[0] if goal_subject_entry else None
            goal_demand=goal_subject_entry[1] if goal_subject_entry else None
            proposals.append(self._p(
                "goal_review",f"Consider whether what just happened changes my goal: {g}",
                "planning.review",{"goal_id":goal_id,"goal":g,"focus":focus,
                "goal_state":goal_states.get(goal.goal_id,{}) if goal.goal_id else {},
                "recent_source_events":recent_source_events,
                "origin_scene":dict((goal.meta or {}).get("origin_scene") or {}),
                "current_scene":{
                    "location":current_scene.get("location"),
                    "present_entities":current_scene.get("present_entities") or [],
                    "immediate_goal":current_scene.get("immediate_goal"),
                    "pending_work":current_scene.get("pending_work"),
                    "last_significant_change":current_scene.get("last_significant_change"),
                },
                "knowledge_demand":goal_demand or {}},
                source_node_id or context.source_head_node_id,context,
                relevance=.45+affinity*.35,
                goal_affinity=max(.35,affinity)+(.15 if starvation else 0),recency=1,
                evidence=[{"kind":"active_goal","goal_id":goal_id,"text":g,
                           "last_reviewed_at":str(reviewed) if reviewed else None}],
                key=f"goal:{goal_id or g.lower()[:100]}",freshness="strict",
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
        budget=max(1,min(limit,16))
        # Goal lifecycle has a reserved downstream slot, so preserve one review
        # through this upstream proposal budget as well. Otherwise high-scoring
        # recall/research/scene proposals can truncate every goal review before
        # OpportunityRouter ever has a chance to reserve one.
        due_review=next(
            (p for p in proposals if p["opportunity_type"]=="goal_review"),None)
        due_formation=next(
            (p for p in proposals if p["opportunity_type"]=="goal_formation"),None)
        reserved=[p for p in (due_review,due_formation) if p is not None]
        budgeted=([p for p in proposals if p not in reserved][
                    :max(0,budget-len(reserved))] + reserved)
        stored=[]
        for p in budgeted:
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
                _json_dumps(p["operation_payload"]),p["source_node_id"],p["source_timeline_id"],
                p["source_state_version"],p["novelty"],p["relevance"],p["urgency"],p["uncertainty"],
                p["goal_affinity"],p["memory_affinity"],p["knowledge_gap"],p["recency"],
                p["priority_score"],p["freshness_policy"],p["supersession_key"],
                _json_dumps(p["evidence"]),p.get("subject_id"))
            if row: stored.append(dict(row))
        return OpportunityBatch(instance_id,tuple(stored))

    @staticmethod
    def _json_object(value: Any) -> dict[str,Any]:
        if isinstance(value,dict):
            return dict(value)
        if isinstance(value,(str,bytes,bytearray)):
            try:
                decoded=json.loads(value)
            except (TypeError,ValueError,json.JSONDecodeError,UnicodeDecodeError):
                return {}
            return dict(decoded) if isinstance(decoded,dict) else {}
        return {}

    def _p(self,typ,label,op,payload,node,context,*,novelty=0,relevance=0,urgency=0,
           uncertainty=0,goal_affinity=0,memory_affinity=0,knowledge_gap=0,recency=0,
           evidence=(),key:str,freshness="contextual",subject_id:UUID|None=None):
        score=(1.7*urgency+1.35*goal_affinity+1.2*relevance+1.1*knowledge_gap+
               .9*memory_affinity+.65*novelty+.45*uncertainty+.55*recency)
        faculty={
            "memory_recall":"reflection",
            "knowledge_gap":"research",
            "goal_review":"planning",
            "goal_formation":"planning",
            "reflection":"reflection",
            "immediate":"executive",
        }.get(str(typ),"executive")
        prepared=self.cognitive_operations.prepare(
            operation_type=str(op), operation_payload=dict(payload or {}), faculty=faculty,
            freshness_policy=freshness, source_node_id=node,
            source_state_version=context.state_version,
        )
        return {"opportunity_type":typ,"natural_language":_clip(label,220),
                "operation_type":prepared.operation_type,
                "operation_payload":dict(prepared.operation_payload),"source_node_id":node,
                "source_timeline_id":context.source_timeline_id,
                "source_state_version":context.state_version,"novelty":novelty,
                "relevance":relevance,"urgency":urgency,"uncertainty":uncertainty,
                "goal_affinity":goal_affinity,"memory_affinity":memory_affinity,
                "knowledge_gap":knowledge_gap,"recency":recency,"priority_score":score,
                "freshness_policy":prepared.freshness_policy,"supersession_key":key,"evidence":list(evidence),
                "subject_id":subject_id}
