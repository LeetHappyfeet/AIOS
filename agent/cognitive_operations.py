from __future__ import annotations

import json
import re
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.pipeline.jobs import enqueue_job
from aios_app.epistemic.research import CorpusSearchService
from aios_app.hud.context import HUDContextResolver


class CognitiveOperationEngine:
    """Execute one bounded cognitive operation; inference is queued, never hidden here."""

    def __init__(self, db: Database):
        self.db = db

    async def create_from_opportunity(self, *, opportunity: Mapping[str, Any],
                                      thread_id: UUID | None, priority: int = 100,
                                      source_task_id: UUID | None = None) -> UUID:
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.character_cognitive_operation(
                   thread_id,instance_id,opportunity_id,operation_type,input,
                   source_state_version,source_timeline_id,source_node_id,freshness_policy,source_task_id)
               VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10) RETURNING operation_id""",
            thread_id, opportunity["instance_id"], opportunity["opportunity_id"],
            opportunity["operation_type"],
            json.dumps(self._mapping(opportunity.get("operation_payload")), default=str),
            opportunity.get("source_state_version"), opportunity.get("source_timeline_id"),
            opportunity.get("source_node_id"), opportunity.get("freshness_policy") or "contextual",
            source_task_id,
        )
        operation_id=row["operation_id"]
        if thread_id:
            await self.db.execute(
                """UPDATE aios.character_cognitive_thread
                   SET status='working',updated_at=now()
                   WHERE thread_id=$1 AND status IN ('open','working')""",thread_id)
        await enqueue_job(self.db,job_type="cognitive_operation",
            payload={"instance_id":str(opportunity["instance_id"]),"operation_id":str(operation_id)},
            priority=priority)
        return operation_id

    async def execute(self, operation_id: UUID) -> None:
        row=await self.db.execute_returning_row(
            """UPDATE aios.character_cognitive_operation
               SET status='running',started_at=COALESCE(started_at,now()),updated_at=now()
               WHERE operation_id=$1 AND status='queued' RETURNING *""",operation_id)
        if not row:
            return
        op=dict(row)
        try:
            kind=str(op["operation_type"])
            if kind=="memory.retrieve":
                result={"kind":"recall","input":self._mapping(op["input"])}
                await self._finish(op,result)
            elif kind=="corpus.search":
                payload=self._mapping(op["input"])
                found=await CorpusSearchService(self.db).search(
                    instance_id=op["instance_id"],query=str(payload.get("query") or ""),limit=8)
                await self._finish(op,{"kind":"inquiry","research_id":str(found.research_id),
                                       "status":found.status,"hits":found.reference_context()})
            elif kind=="inquiry.resolve":
                from aios_app.epistemic.inquiry.contracts import InquiryDemand
                from aios_app.epistemic.inquiry.service import CharacterInquiryService
                payload=self._mapping(op["input"])
                if isinstance(payload.get("demand"), dict):
                    demand=InquiryDemand.from_dict(payload["demand"])
                    if demand.instance_id != op["instance_id"]:
                        raise PermissionError("inquiry instance mismatch")
                else:
                    question=str(payload.get("query") or payload.get("focus") or "").strip()[:600]
                    demand=InquiryDemand(
                        instance_id=op["instance_id"], source_node_id=op.get("source_node_id"),
                        origin="character_cognition", uncertainty_kind="explicit_question",
                        question=question, evidence_scope="character_accessible",
                        evidence_revision=f"{op.get('source_state_version')}:{op.get('source_node_id')}")
                service=CharacterInquiryService(self.db)
                receipt=await service.resolve(
                    demand,allow_model=bool(payload.get("allow_model",False)))
                if (receipt.get("plan_eligible") and
                        await service.claim_plan(UUID(receipt["inquiry_id"]))):
                    await self.db.execute(
                        """UPDATE aios.character_cognitive_operation SET
                           status='waiting_inference',result=$2::jsonb,updated_at=now()
                           WHERE operation_id=$1 AND status='running'""",
                        op["operation_id"],json.dumps(receipt))
                    job_id=await enqueue_job(
                        self.db,job_type="character_inquiry_inference",
                        payload={"instance_id":str(op["instance_id"]),
                                 "operation_id":str(op["operation_id"])},priority=140)
                    if job_id is None:
                        await self.db.execute(
                            """UPDATE aios.character_inquiry SET status=$2,updated_at=now()
                               WHERE inquiry_id=$1 AND status='planning'""",
                            UUID(receipt["inquiry_id"]),receipt["status"])
                        await self.db.execute(
                            """UPDATE aios.character_cognitive_operation SET status='running'
                               WHERE operation_id=$1 AND status='waiting_inference'""",
                            op["operation_id"])
                        await self._finish(op,{"kind":"inquiry",**receipt,
                                              "planner_status":"not_queued"})
                else:
                    await self._finish(op,{"kind":"inquiry",**receipt})
            elif kind in {"reflection.review","planning.review","executive.review"}:
                await self._queue_decision(op)
            elif kind=="planning.form_goal":
                job_id=await enqueue_job(self.db,job_type="goal_formulation_inference",
                    payload={"instance_id":str(op["instance_id"]),
                             "operation_id":str(op["operation_id"])},priority=120)
                if job_id is None:
                    raise RuntimeError("could not enqueue goal formulation")
            else:
                await self._finish(op,{"kind":"unsupported","operation_type":kind})
        except Exception as exc:
            await self._fail(op, exc)
            raise

    async def complete_inquiry(self, operation_id: UUID) -> None:
        """Run the optional planner off the deterministic cognitive-operation lane."""
        row=await self.db.fetchrow(
            """SELECT * FROM aios.character_cognitive_operation
               WHERE operation_id=$1 AND status='waiting_inference'
                 AND operation_type='inquiry.resolve'""",operation_id)
        if not row:
            return
        op=dict(row)
        receipt=self._mapping(op.get("result"))
        if not receipt.get("inquiry_id"):
            await self._fail(op,ValueError("missing inquiry audit identifier"))
            return
        from aios_app.epistemic.inquiry.service import CharacterInquiryService
        result=await CharacterInquiryService(self.db).plan_and_retry(
            UUID(str(receipt["inquiry_id"])))
        changed=await self.db.execute_returning_row(
            """UPDATE aios.character_cognitive_operation SET status='running',
                      updated_at=now()
               WHERE operation_id=$1 AND status='waiting_inference'
               RETURNING operation_id""",operation_id)
        if changed:
            await self._finish(op,{"kind":"inquiry",**result})

    async def form_goal(self, operation_id: UUID) -> None:
        """One bounded planning inference, executed in the isolated inference lane."""
        row=await self.db.fetchrow(
            "SELECT * FROM aios.character_cognitive_operation WHERE operation_id=$1",
            operation_id)
        if not row or row["status"]!="running" or row["operation_type"]!="planning.form_goal":
            return
        op=dict(row)
        try:
            context=await HUDContextResolver(self.db).resolve(op["instance_id"])
            if (context.source_timeline_id != op["source_timeline_id"] or
                context.source_head_node_id != op["source_node_id"] or
                context.state_version != op["source_state_version"]):
                await self._stale(op,"goal formation context advanced")
                return
            source=await self.db.fetchrow(
                "SELECT message_text,speaker_role,speaker_id FROM aios.dag_node WHERE node_id=$1",
                op["source_node_id"])
            if not source or not source["message_text"]:
                await self._finish(op,{"kind":"no_goal","reason":"no source evidence"})
                return
            from aios_app.inference.broker import InferenceBroker, InferenceRequest
            from aios_app.epistemic.goals import CharacterGoalService
            source_text=str(source["message_text"])[:1800]
            existing=await CharacterGoalService(self.db).resolve_active(op["instance_id"])
            prompt=(
                "Decide whether the character should set ONE concrete, actionable objective "
                "based on the current event. Return JSON with exactly goal (string or null) "
                "and horizon (scene, session, or persistent). A user suggestion is not an "
                "accepted character commitment: for an unaccepted invitation only propose "
                "an objective to decide or respond to it. Do not invent an activity, desire, "
                "past event, or commitment. Return null if there is no worthwhile new "
                "objective or it duplicates an active goal. Persistent requires an explicit "
                "future commitment made by the character in this event. Keep goal under 140 characters.\n"
                f"Speaker role: {source['speaker_role']}\n"
                f"Speaker: {source['speaker_id']}\n"
                f"Active goals: {[g.text for g in existing.active[:5]]}\n"
                f"Current event: {source_text}"
            )
            inference=await InferenceBroker(self.db).infer(InferenceRequest(
                instance_id=op["instance_id"],worker_class="planning",prompt=prompt,
                context_state_version=context.state_version,allowed_actions={},
                output_schema={"goal":"string or null","horizon":"scene|session|persistent"},
                max_tokens=160))
            raw=inference.response.raw
            goal=raw.get("goal")
            if goal is None:
                await self._finish(op,{"kind":"no_goal"})
                return
            if not isinstance(goal,str) or len(goal.strip())<12 or len(goal)>140:
                await self._finish(op,{"kind":"rejected_goal","reason":"invalid text"})
                return
            # The inference can formulate an objective, but cannot introduce
            # unrelated people, places, or activities into the character state.
            tokens=set(re.findall(r"[a-z]{4,}",goal.lower()))
            evidence=set(re.findall(r"[a-z]{4,}",source_text.lower()))
            if len(tokens & evidence)<2 or any(g.text.casefold()==goal.strip().casefold()
                                                for g in existing.active):
                await self._finish(op,{"kind":"rejected_goal","reason":"ungrounded or duplicate"})
                return
            horizon=str(raw.get("horizon") or "session")
            if horizon not in {"scene","session","persistent"}:
                horizon="session"
            if source["speaker_role"] not in {"character","assistant"}:
                horizon="session" if horizon=="persistent" else horizon
            # Recheck after inference. A new turn may have arrived meanwhile.
            current=await HUDContextResolver(self.db).resolve(op["instance_id"])
            if (current.source_head_node_id != op["source_node_id"] or
                current.state_version != op["source_state_version"]):
                await self._stale(op,"goal formation context advanced")
                return
            created=await CharacterGoalService(self.db).create(
                instance_id=op["instance_id"],text=goal.strip(),
                source_node_id=op["source_node_id"],
                meta={"created_by":"planning_formation","horizon":horizon,
                      "source_operation_id":str(operation_id)})
            await self._finish(op,{"kind":"formed_goal","goal_id":str(created.goal_id)})
        except Exception as exc:
            await self._fail(op,exc)
            raise

    async def _queue_decision(self, op: Mapping[str,Any]) -> None:
        from aios_app.agent.transactions import InternalCognitionTransactions
        payload=self._mapping(op["input"])
        subject=" ".join(str(v) for v in payload.values() if v)[:220]
        labels={
            "reflection.review":[
                f"This seems important; keep it in mind: {subject}.",
                f"I may be reading too much into this: {subject}.",
                f"This changes how I understand the situation: {subject}.",
                "I do not have enough evidence to decide what it means."],
            "planning.review":[
                "The goal remains unresolved and its opportunity is still open.",
                "I made progress, but the goal remains active.",
                "The available evidence satisfies this goal.",
                "The original opportunity to complete this goal has passed without satisfaction."],
            "executive.review":[
                "This deserves immediate attention.",
                "I should wait for more information.",
                "I should preserve this concern but not act yet.",
                "This no longer needs action."],
        }[str(op["operation_type"])]
        choice_freshness="strict" if str(op["operation_type"])=="executive.review" else str(op.get("freshness_policy") or "contextual")
        candidates=[{"key":k,"label":label,"operation":"operation_choice",
                     "operation_id":str(op["operation_id"]),"option_index":i,
                     "freshness_policy":choice_freshness}
                    for i,(k,label) in enumerate(zip("ABCD",labels))]
        fifth=("I still could pursue this goal, but it does not deserve active attention right now."
               if str(op["operation_type"])=="planning.review"
               else "Stop considering this for now.")
        candidates.append({"key":"E","label":fifth,
                           "operation":"operation_choice","operation_id":str(op["operation_id"]),
                           "option_index":4,"freshness_policy":choice_freshness})
        faculty_profile={
            "reflection.review":"reflection",
            "planning.review":"planning",
            "executive.review":"executive",
        }.get(str(op["operation_type"]),"attention")
        tx=await InternalCognitionTransactions(self.db).create(
            instance_id=op["instance_id"], candidates=candidates, priority=120,
            ttl_seconds=300, worker_profile=faculty_profile,
            focus_text=subject, subject=subject)
        await self.db.execute(
            """UPDATE aios.character_cognitive_operation SET status='waiting_inference',
               transaction_id=$2,updated_at=now() WHERE operation_id=$1""",
            op["operation_id"],tx)
        await enqueue_job(self.db,job_type="internal_cognition_inference",
            payload={"instance_id":str(op["instance_id"]),"transaction_id":str(tx),
                     "operation_id":str(op["operation_id"])},priority=120)

    async def accept_choice(self, operation_id: UUID, selected: Mapping[str,Any]) -> bool:
        row=await self.db.fetchrow(
            "SELECT * FROM aios.character_cognitive_operation WHERE operation_id=$1",operation_id)
        if not row or row["status"]!="waiting_inference":
            return False
        op=dict(row)
        current=await HUDContextResolver(self.db).resolve(op["instance_id"])
        if op.get("source_timeline_id") and current.source_timeline_id != op["source_timeline_id"]:
            await self._stale(op,"source timeline changed while inference was running")
            return False
        policy=str(op.get("freshness_policy") or "contextual")
        if str(op["operation_type"])=="executive.review":
            policy="strict"
        if policy=="strict" and (
            current.state_version != op.get("source_state_version")
            or current.source_head_node_id != op.get("source_node_id")
        ):
            await self._stale(op,"strict operation context advanced while inference was running")
            return False
        result={"kind":"choice","option_index":selected.get("option_index"),
                "label":selected.get("label")}
        changed=await self.db.execute_returning_row(
            """UPDATE aios.character_cognitive_operation
               SET status='succeeded',result=$2::jsonb,completed_at=now(),updated_at=now()
               WHERE operation_id=$1 AND status='waiting_inference' RETURNING operation_id""",
            operation_id,json.dumps(result,default=str))
        if not changed:
            return False
        await self._finish_side_effects(op,result)
        return True

    async def _stale(self, op: Mapping[str,Any], reason: str) -> None:
        changed=await self.db.execute_returning_row(
            """UPDATE aios.character_cognitive_operation SET status='stale',result=$2::jsonb,
               completed_at=now(),updated_at=now()
               WHERE operation_id=$1 AND status IN ('queued','running','waiting_inference')
               RETURNING operation_id""",
            op["operation_id"],json.dumps({"reason":reason}))
        if changed:
            await self._terminal_side_effects(op,status="stale",result={"reason":reason})
            await self._rearm_failed_episode(op)

    async def _fail(self, op: Mapping[str,Any], exc: Exception) -> None:
        result={"error":str(exc)[:1000],"error_type":type(exc).__name__}
        changed=await self.db.execute_returning_row(
            """UPDATE aios.character_cognitive_operation SET status='failed',
               result=$2::jsonb,completed_at=now(),updated_at=now()
               WHERE operation_id=$1 AND status IN ('queued','running','waiting_inference')
               RETURNING operation_id""",
            op["operation_id"],json.dumps(result))
        if changed:
            await self._terminal_side_effects(op,status="failed",result=result)
            await self._rearm_failed_episode(op)

    async def _rearm_failed_episode(self, op: Mapping[str,Any]) -> None:
        """Retry only unacknowledged host-delta work, never arbitrary cognition."""
        source_node_id=op.get("source_node_id")
        if not source_node_id:
            return
        runtime=await self.db.fetchrow(
            """SELECT pending_cognitive_source_node_id,pending_cognitive_event_count,
                      cognitive_batch_size
               FROM aios.character_agent_runtime WHERE instance_id=$1""",
            op["instance_id"])
        if not runtime:
            return
        # A newer host delta may already have superseded this operation. Only
        # retry the exact still-pending episode and only while it remains due.
        if (runtime["pending_cognitive_source_node_id"] != source_node_id
                or int(runtime["pending_cognitive_event_count"]) <
                   int(runtime["cognitive_batch_size"])):
            return
        previous=await self.db.fetchval(
            """SELECT COALESCE(max((payload->>'retry_attempt')::int),0)
               FROM aios.character_wake_event
               WHERE instance_id=$1 AND event_type='COGNITIVE_DELTA_READY'
                 AND source_id=$2 AND payload ? 'retry_attempt'""",
            op["instance_id"],str(source_node_id))
        from aios_app.agent.runtime import AgentRuntimeStore
        await AgentRuntimeStore(self.db).rearm_cognitive_delta(
            instance_id=op["instance_id"],source_node_id=source_node_id,
            retry_attempt=int(previous or 0)+1,max_attempts=3)

    async def _terminal_side_effects(
        self, op: Mapping[str,Any], *, status: str, result: Mapping[str,Any]
    ) -> None:
        if op.get("opportunity_id"):
            await self.db.execute(
                """UPDATE aios.character_cognitive_opportunity
                   SET status='suppressed',resolved_at=now(),updated_at=now()
                   WHERE opportunity_id=$1 AND status IN ('pending','offered','selected')""",
                op["opportunity_id"])
        from aios_app.agent.cognitive_lifecycle import CognitiveLifecycleReconciler
        await CognitiveLifecycleReconciler(self.db).reconcile_operation(
            operation=op,result=result,terminal_status=status)

    async def _finish(self, op: Mapping[str,Any], result: Mapping[str,Any]) -> None:
        changed=await self.db.execute_returning_row(
            """UPDATE aios.character_cognitive_operation SET status='succeeded',result=$2::jsonb,
               completed_at=now(),updated_at=now()
               WHERE operation_id=$1 AND status='running' RETURNING operation_id""",
            op["operation_id"],json.dumps(result,default=str))
        if not changed:
            return
        await self._finish_side_effects(op,result)

    async def _finish_side_effects(self, op: Mapping[str,Any], result: Mapping[str,Any]) -> None:
        if op.get("operation_type")=="inquiry.resolve":
            # Searching is not goal progress or source admission. Still close
            # the opportunity and record the thread receipt so it cannot stick
            # in 'selected' or 'working' indefinitely.
            if op.get("opportunity_id"):
                await self.db.execute(
                    """UPDATE aios.character_cognitive_opportunity
                       SET status='executed',resolved_at=now(),updated_at=now()
                       WHERE opportunity_id=$1""",op["opportunity_id"])
            from aios_app.agent.cognitive_lifecycle import CognitiveLifecycleReconciler
            await CognitiveLifecycleReconciler(self.db).reconcile_operation(
                operation=op,result=result,terminal_status="inquiry_observation")
            await self._resume_source_task(op,status="succeeded",result=result)
            return
        if op.get("source_node_id"):
            from aios_app.agent.admission import AutonomyAdmissionService
            await AutonomyAdmissionService(self.db).mark_episode_succeeded(
                instance_id=op["instance_id"], through_node_id=op["source_node_id"])
        if op.get("opportunity_id"):
            await self.db.execute(
                """UPDATE aios.character_cognitive_opportunity SET status='executed',
                   resolved_at=now(),updated_at=now() WHERE opportunity_id=$1""",op["opportunity_id"])
        from aios_app.agent.cognitive_lifecycle import CognitiveLifecycleReconciler
        await CognitiveLifecycleReconciler(self.db).reconcile_operation(
            operation=op,result=result,terminal_status="succeeded")
        await self._resume_source_task(op, status="succeeded", result=result)

    async def _resume_source_task(self, op: Mapping[str,Any], *, status: str,
                                  result: Mapping[str,Any]) -> None:
        task_id=op.get("source_task_id")
        if not task_id:
            return
        from .lifecycle import CharacterAgencyStore
        agency=CharacterAgencyStore(self.db)
        task=await agency.get_task(task_id)
        if not task or task.status!="waiting":
            return
        receipt={"operation_id":str(op["operation_id"]),"operation_type":op["operation_type"],
                 "status":status,"result":dict(result)}
        if status=="succeeded":
            await agency.transition_task(task_id,"succeeded",result=receipt)
            event_type="COGNITIVE_TASK_COMPLETED"
        else:
            await agency.transition_task(task_id,"failed",error=str(result)[:1000])
            event_type="COGNITIVE_TASK_FAILED"
        if task.parent_task_id:
            from .runtime import AgentRuntimeStore
            await AgentRuntimeStore(self.db).wake(
                instance_id=task.instance_id,event_type=event_type,
                source_type="cognitive_task",source_id=str(task.task_id),
                payload={"task_id":str(task.task_id),"parent_task_id":str(task.parent_task_id),
                         "status":status,"result":receipt},
                priority=max(1,task.priority-1),
                dedupe_key=f"task:{task.task_id}:{status}")

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        current=value
        for _ in range(2):
            if isinstance(current,Mapping):
                return dict(current)
            if isinstance(current,(bytes,bytearray)):
                try:
                    current=current.decode("utf-8")
                except UnicodeDecodeError:
                    return {}
            if not isinstance(current,str):
                return {}
            try:
                current=json.loads(current)
            except (TypeError,ValueError,json.JSONDecodeError):
                return {}
        return dict(current) if isinstance(current,Mapping) else {}
