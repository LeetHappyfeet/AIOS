from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService, valid_goal_objective
from aios_app.epistemic.message_cognition import cognition_topic_key
from aios_app.inference import InferenceBroker, InferenceRequest, InferenceUnavailable
from aios_app.config import settings
from aios_app.agent.temporal import TemporalTriggerStore, resolve_goal_time_expression


_ALLOWED_KINDS={"GOAL","BELIEF","STATE","RELATIONSHIP","RULE","EVENT"}
_FUTURE_TIME_RE = re.compile(
    r"\b(?:tomorrow|later|tonight|next\s+(?:day|week|month|year)|"
    r"this\s+(?:afternoon|evening|week|weekend)|in\s+\d+\s+(?:minutes?|hours?|days?|weeks?))\b",
    re.I,
)


def _effective_goal_horizon(horizon: str, intent_type: str, excerpt: str) -> str:
    """Do not demote an explicitly future commitment to an immediate act."""
    if (horizon == "immediate" and intent_type in {
            "desire", "objective", "plan", "commitment", "immediate_intention"}
            and _FUTURE_TIME_RE.search(excerpt)):
        return "session"
    return horizon


def _specific_commitment_objective(objective: str, intent_type: str) -> bool:
    """Reject one-word commitment objects such as an ungrounded ``go``."""
    if intent_type != "commitment":
        return True
    return len(re.findall(r"[A-Za-z0-9]+", objective)) >= 2


class MessageCognitionEnricher:
    """Bounded LLM adjudication for semantic units missed by the cheap parser.

    The model has classification authority only. Database admission, ownership,
    lifecycle and goal creation remain deterministic.
    """
    def __init__(self,db: Database):
        self.db=db
        self.broker=InferenceBroker(db)

    async def run(self,*,instance_id: UUID,node_id: UUID) -> int:
        row=await self.db.fetchrow(
            """SELECT c.commit_id,c.summary,c.character_id,c.speaker_id,c.speaker_role,
                      c.viewpoint_id,c.event_id
               FROM aios.message_cognitive_commit c
               WHERE c.instance_id=$1 AND c.node_id=$2""",instance_id,node_id)
        if not row:
            return 0
        source_clock = await self.db.fetchrow(
            """SELECT dn.created_at,ci.meta
               FROM aios.dag_node dn
               JOIN aios.character_instance ci ON ci.instance_id=$1
               WHERE dn.node_id=$2""",
            instance_id, node_id,
        )
        summary=CharacterGoalService._json_object(row["summary"])
        sentences=list(summary.get("ambiguous_sentences") or [])[:4]
        if not sentences or not summary.get("enrichment_pending"):
            return 0

        # A short character reply may refer to a plan offered in the parent
        # turn. Read only the actual DAG parent and expose it as neutral context.
        parent = await self.db.fetchrow(
            """SELECT parent_node.speaker_id,parent_node.message_text
               FROM aios.dag_node child_node
               JOIN aios.dag_edge de
                 ON de.timeline_id=child_node.timeline_id
                AND de.child_node_id=child_node.node_id
               JOIN aios.dag_node parent_node
                 ON parent_node.timeline_id=de.timeline_id
                AND parent_node.node_id=de.parent_node_id
               WHERE child_node.node_id=$1
               ORDER BY de.created_at DESC
               LIMIT 1""",
            node_id,
        )
        parent_context = ""
        if parent and parent["message_text"]:
            parent_context = (
                f"Prior turn by {parent['speaker_id'] or 'unknown speaker'} "
                "(context only; do not attribute its statements to the character):\n"
                f"{str(parent['message_text'])[-1800:]}\n"
            )

        prompt=(
            "Classify only durable or cognitively meaningful information expressed by the "
            "character in these excerpts. Do not invent motives. A GOAL requires an intention, "
            "commitment, plan, chosen objective, or persistent desire belonging to the character; "
            "requests for another person to act are not the character's goal unless the character "
            "is explicitly trying to cause that outcome. Distinguish BELIEF, STATE, RELATIONSHIP, "
            "RULE, EVENT, GOAL, or NONE. For GOAL only, also classify intent_type as "
            "desire|objective|plan|commitment|immediate_intention and horizon as "
            "immediate|scene|session|persistent, and provide objective as a concise but "
            "specific content phrase. A commitment with a future time such as tomorrow, "
            "later, or next week is a session goal, not an immediate action. Use prior-turn "
            "context only to resolve omitted referents; classify only what the character "
            "commits to in the excerpts. For each GOAL include schedule with decision "
            "schedule|none and an expression. Choose schedule only for a clear future plan "
            "or commitment owned by the character, with a specific time phrase in that same "
            "character excerpt. Do not schedule a time proposed only by another speaker, a "
            "desire without a plan, or a vague time unless nearby context supports a revisit. "
            "For later, soon, afterwards, or sometime later, you may choose delay_seconds "
            "from exactly 900, 3600, 14400, or 86400; choose none if no interval is justified. "
            "Return {\\\"units\\\":[{\\\"kind\\\":...,"
            "\\\"text\\\":...,\\\"polarity\\\":1|-1,\\\"confidence\\\":0..1,"
            "\\\"source_index\\\":0..N,\\\"intent_type\\\":...,\\\"horizon\\\":...,"
            "\\\"objective\\\":...,\\\"schedule\\\":{\\\"decision\\\":...,"
            "\\\"expression\\\":...,\\\"delay_seconds\\\":...}}]}. "
            "At most 4 units.\n"
            f"Character: {row['character_id']}\nSpeaker: {row['speaker_id']}\n"
            + parent_context
            + "Excerpts:\n"+ "\n".join(f"[{i}] {s}" for i,s in enumerate(sentences))
        )
        try:
            result=await self.broker.infer(InferenceRequest(
                instance_id=instance_id,worker_class="message_cognition",
                prompt=prompt,allowed_actions={},output_schema={"type":"object"},
                temperature=0.0,max_tokens=450))
        except InferenceUnavailable:
            # Optional enrichment must never make deterministic cognition fail.
            return 0

        raw=result.response.raw
        units=raw.get("units") if isinstance(raw,dict) else None
        admitted=0
        if isinstance(units,list):
            for item in units[:4]:
                if not isinstance(item,dict):
                    continue
                kind=str(item.get("kind") or "").upper()
                text=" ".join(str(item.get("text") or "").split())[:500]
                try: confidence=float(item.get("confidence",0))
                except (TypeError,ValueError): confidence=0
                try: source_index=int(item.get("source_index",-1))
                except (TypeError,ValueError): source_index=-1
                polarity=-1 if int(item.get("polarity",1) or 1)<0 else 1
                if kind not in _ALLOWED_KINDS or not text or confidence<0.72:
                    continue
                if source_index<0 or source_index>=len(sentences):
                    continue
                intent_type=str(item.get("intent_type") or "").lower() if kind=="GOAL" else ""
                horizon=str(item.get("horizon") or "").lower() if kind=="GOAL" else ""
                objective=" ".join(str(item.get("objective") or "").split())[:300] if kind=="GOAL" else ""
                if kind=="GOAL":
                    horizon = _effective_goal_horizon(
                        horizon, intent_type, sentences[source_index]
                    )
                    if intent_type not in {"desire","objective","plan","commitment","immediate_intention"}:
                        continue
                    if (horizon not in {"immediate","scene","session","persistent"}
                            or not valid_goal_objective(objective)
                            or not _specific_commitment_objective(objective,intent_type)):
                        continue
                schedule = item.get("schedule") if kind == "GOAL" else None
                schedule_decision = (
                    str(schedule.get("decision") or "none").lower()
                    if isinstance(schedule, dict) else "none"
                )
                schedule_expression = (
                    " ".join(str(schedule.get("expression") or "").split())[:80]
                    if isinstance(schedule, dict) else ""
                )
                try:
                    schedule_delay = int(schedule.get("delay_seconds")) if isinstance(schedule, dict) else None
                except (TypeError, ValueError):
                    schedule_delay = None
                topic=cognition_topic_key(
                    text, character_id=str(row["character_id"]),
                    owner=str(row["character_id"]), kind=kind,
                    objective=objective or None)
                unit=await self.db.execute_returning_row(
                    """INSERT INTO aios.message_cognitive_unit(
                         commit_id,ordinal,claim_kind,text,topic_key,polarity,
                         salience,confidence,status,meta)
                       SELECT $1,COALESCE(max(ordinal),-1)+1,$2,$3,$4,$5,
                              CASE WHEN $2='GOAL' THEN .86 ELSE .72 END,$6,'active',$7::jsonb
                       FROM aios.message_cognitive_unit WHERE commit_id=$1
                       HAVING count(*) FILTER (
                           WHERE claim_kind=$2::text
                             AND meta->>'source'='bounded_inference'
                             AND meta->>'source_index'=$8::text
                       )=0
                       ON CONFLICT (commit_id,ordinal) DO NOTHING
                       RETURNING unit_id,topic_key,text""",
                    row["commit_id"],kind,text,topic,polarity,confidence,
                    json.dumps({"character_owned":True,"semantic_owner":str(row["character_id"]),
                                "source":"bounded_inference","source_index":source_index,
                                "source_text":sentences[source_index],
                                "inference_request_id":str(result.request_id),
                                "intent_type":intent_type or None,"horizon":horizon or None,
                                "objective":objective or None,
                                "temporal_proposal":({"decision":schedule_decision,
                                    "expression":schedule_expression or None,
                                    "delay_seconds":schedule_delay}
                                    if schedule_decision == "schedule" else None)}),str(source_index))
                if not unit:
                    # A previous attempt may have committed the semantic unit
                    # and failed before creating its timer. Reuse that unit so
                    # timer admission is retried idempotently.
                    unit = await self.db.fetchrow(
                        """SELECT unit_id,topic_key,text FROM aios.message_cognitive_unit
                           WHERE commit_id=$1 AND claim_kind=$2
                             AND meta->>'source'='bounded_inference'
                             AND meta->>'source_index'=$3
                           ORDER BY created_at DESC LIMIT 1""",
                        row["commit_id"], kind, str(source_index),
                    )
                    if not unit and kind == "GOAL":
                        # Deterministic character-owned goals need only temporal
                        # adjudication here; keep their original unit and avoid
                        # inserting a duplicate claim.
                        unit = await self.db.fetchrow(
                            """SELECT unit_id,topic_key,text FROM aios.message_cognitive_unit
                               WHERE commit_id=$1 AND claim_kind='GOAL'
                                 AND status='active'
                                 AND COALESCE((meta->>'character_owned')::boolean,false)
                                 AND btrim(meta->>'source_text')=btrim($2)
                               ORDER BY ordinal LIMIT 1""",
                            row["commit_id"], sentences[source_index],
                        )
                    if not unit:
                        continue
                    topic = str(unit["topic_key"] or topic)
                    text = str(unit["text"] or text)
                else:
                    admitted += 1
                if kind=="GOAL" and horizon not in {"immediate"}:
                    goal = await CharacterGoalService(self.db).reconcile_evidence(
                        instance_id=instance_id,text=text,topic_key=topic,polarity=polarity,
                        source_node_id=node_id,source_unit_id=unit["unit_id"],
                        confidence=confidence,salience=.86,intent_type=intent_type,
                        horizon=horizon,objective=objective)
                    if (goal is not None and polarity > 0 and goal.goal_id is not None
                            and schedule_decision == "schedule"
                            and intent_type in {"objective", "plan", "commitment"}
                            and row["speaker_role"] in {"character", "assistant"}
                            and confidence >= .80 and source_clock
                            and source_clock["created_at"] is not None):
                        instance_meta = CharacterGoalService._json_object(source_clock["meta"])
                        zone = instance_meta.get("timezone") or settings.default_timezone
                        window = resolve_goal_time_expression(
                            expression=schedule_expression,
                            source_text=sentences[source_index],
                            source_at=source_clock["created_at"],
                            timezone_name=str(zone) if zone else None,
                            suggested_delay_seconds=schedule_delay,
                        )
                        if window is not None:
                            await TemporalTriggerStore(self.db).schedule_goal_review(
                                instance_id=instance_id, goal_id=goal.goal_id,
                                due_at=window.due_at, window_end_at=window.window_end_at,
                                timezone_name=window.timezone_name,
                                reason=f"Review future goal: {goal.text[:120]}",
                                payload={"time_expression":window.expression,
                                         "source_node_id":str(node_id),
                                         "source_unit_id":str(unit["unit_id"]),
                                         "resolution_policy":"character_future_commitment_v1"},
                                dedupe_key=f"goal-review:{goal.goal_id}:{node_id}",
                            )

        await self.db.execute(
            """UPDATE aios.message_cognitive_commit
               SET summary=summary || jsonb_build_object(
                   'enrichment_pending',false,'enrichment_request_id',$3::text,
                   'enrichment_admitted',$4::integer),
                   enrichment_completed_at=now()
               WHERE instance_id=$1 AND node_id=$2""",
            instance_id,node_id,str(result.request_id),admitted)
        return admitted
