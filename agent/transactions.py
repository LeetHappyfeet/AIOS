from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from aios_app.db import Database
from aios_app.hud.internal_frame import InternalHUDAssembler, InternalHUD
from aios_app.inference.broker import InferenceBroker, InferenceRequest


DEFAULT_CHOICES = (
    {"key":"A","label":"Recall something relevant from my memory.","operation":"memory"},
    {"key":"B","label":"Look up information I am missing.","operation":"research"},
    {"key":"C","label":"Plan or reconsider what I should do next.","operation":"planning"},
    {"key":"D","label":"Handle an urgent or immediate concern.","operation":"urgent"},
    {"key":"E","label":"Nothing needs background attention right now.","operation":"wait"},
)


@dataclass(frozen=True)
class TransactionResult:
    transaction_id: UUID
    status: str
    choice: str | None
    operation: str | None
    focus: str | None


class InternalCognitionTransactions:
    """One inference, one receipt, then disposal.

    The LLM selects only among prepared choices. It cannot emit an action.
    """

    def __init__(self, db: Database):
        self.db = db
        self.huds = InternalHUDAssembler(db)
        self.broker = InferenceBroker(db)

    async def create(
        self, *, instance_id: UUID, clues: Sequence[str] = (),
        candidates: Sequence[Mapping[str, Any]] = DEFAULT_CHOICES,
        priority: int = 150, ttl_seconds: int = 300,
        source_task_id: UUID | None = None,
        opportunity_ids: Sequence[UUID] = (),
        worker_profile: str = "attention",
        focus_text: str | None = None,
        subject: str | None = None,
        episode_id: UUID | None = None, transaction_kind: str = "route",
        review_context: Mapping[str, Any] | None = None,
    ) -> UUID:
        choices = [dict(x) for x in candidates][:5]
        if review_context is not None:
            from .goal_review import review_prompt
            context = await self.huds.contexts.resolve(instance_id)
            prompt = review_prompt(review_context, choices)
            hud = InternalHUD(prompt=prompt, state_version=context.state_version,
                source_timeline_id=context.source_timeline_id,
                source_node_id=context.source_head_node_id,
                fingerprint=hashlib.sha256(prompt.encode()).hexdigest())
        else:
            hud = await self.huds.build(
                instance_id, clues=clues, candidates=choices,
                worker_profile=worker_profile, focus_text=focus_text, subject=subject,
            )
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.internal_cognition_transaction(
                 instance_id,source_task_id,priority,source_state_version,
                 source_timeline_id,source_node_id,context_fingerprint,clues,
                 candidates,prompt_text,prompt_hash,expires_at,opportunity_ids,episode_id,transaction_kind)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,
                      now()+($12*interval '1 second'),$13::uuid[],$14,$15)
               RETURNING transaction_id""",
            instance_id, source_task_id, int(priority), hud.state_version,
            hud.source_timeline_id, hud.source_node_id, hud.fingerprint,
            json.dumps(list(clues)[:2]), json.dumps(choices), hud.prompt,
            hashlib.sha256(hud.prompt.encode()).hexdigest(),
            max(10,min(int(ttl_seconds),3600)), list(opportunity_ids),episode_id,transaction_kind,
        )
        return row["transaction_id"]

    async def run(self, transaction_id: UUID) -> TransactionResult:
        row = await self.db.execute_returning_row(
            """UPDATE aios.internal_cognition_transaction SET status='running',
                 claimed_at=now(),updated_at=now()
               WHERE transaction_id=$1 AND status='pending' AND expires_at>now()
               RETURNING *""", transaction_id,
        )
        if not row:
            current=await self.db.fetchrow(
                "SELECT * FROM aios.internal_cognition_transaction WHERE transaction_id=$1",transaction_id)
            if current and current["status"] == "pending":
                return await self._reject(transaction_id,"stale","expired before claim")
            if current:
                await self.reconcile_terminal(transaction_id)
                return TransactionResult(transaction_id,current["status"],current["choice_key"],None,None)
            return TransactionResult(transaction_id,"stale",None,None,None)

        tx=dict(row); candidates=self._json(tx["candidates"],[])
        try:
            inference=await self.broker.infer(InferenceRequest(
                instance_id=tx["instance_id"], task_id=tx["source_task_id"],
                worker_class="executive", prompt=tx["prompt_text"],
                context_state_version=tx["source_state_version"],
                hud_profile_name=tx["hud_profile_name"],
                allowed_actions={}, output_schema=None,
                choice_keys=tuple(str(x.get("key","")).upper() for x in candidates if x.get("key")),
                max_tokens=8,
            ))
            raw=inference.response.raw
            if set(raw) != {"choice"} or not isinstance(raw.get("choice"),str):
                return await self._reject(
                    transaction_id,"invalid","micro inference must return exactly one string field: choice")
            choice=raw["choice"].strip().upper()
            if len(choice) != 1:
                return await self._reject(transaction_id,"invalid",f"invalid choice {choice!r}")
            focus=None
            selected=next((x for x in candidates if str(x.get("key","")).upper()==choice),None)
            if not selected:
                return await self._reject(transaction_id,"invalid",f"unknown choice {choice!r}")
            await self.db.execute(
                """UPDATE aios.internal_cognition_transaction SET status='proposed',
                     inference_request_id=$2,choice_key=$3,choice_focus=$4,updated_at=now()
                   WHERE transaction_id=$1 AND status='running'""",
                transaction_id,inference.request_id,choice,focus,
            )
            timely,reason=await self._timely(tx,selected)
            if not timely:
                return await self._reject(transaction_id,"stale",reason,choice,selected.get("operation"),focus)
            async with self.db.atomic():
                owned=await self.db.fetchrow(
                    "SELECT status FROM aios.internal_cognition_transaction WHERE transaction_id=$1 FOR UPDATE",
                    transaction_id)
                if not owned or owned["status"] != "proposed":
                    return TransactionResult(transaction_id,"stale",choice,None,None)
                timely,reason=await self._timely(tx,selected)
                if not timely:
                    return await self._reject(transaction_id,"stale",reason,choice,selected.get("operation"),focus)
                materialized=await self._materialize(tx,selected,focus)
                if materialized.get("kind") == "stale_opportunity" or materialized.get("accepted") is False:
                    return await self._reject(transaction_id,"stale","choice no longer admissible",choice)
                result={"operation":selected.get("operation"),"focus":focus,"choice":choice,
                        "materialized":materialized}
                await self.db.execute(
                    """UPDATE aios.internal_cognition_transaction SET status='consumed',
                         result=$2::jsonb,completed_at=now(),consumed_at=now(),updated_at=now()
                       WHERE transaction_id=$1 AND status='proposed'""",
                    transaction_id,json.dumps(result))
            await self.reconcile_terminal(transaction_id)
            return TransactionResult(transaction_id,"consumed",choice,str(selected.get("operation")),focus)
        except Exception as exc:
            await self._reject(transaction_id,"failed",str(exc)[:1000])
            raise

    async def _materialize(self, tx: Mapping[str,Any], selected: Mapping[str,Any],
                           focus: str | None) -> Mapping[str,Any]:
        operation=str(selected.get("operation") or "wait")
        offered=list(tx.get("opportunity_ids") or [])
        if operation == "wait":
            if offered:
                await self.db.execute(
                    """UPDATE aios.character_cognitive_opportunity
                       SET status='suppressed',resolved_at=now(),updated_at=now()
                       WHERE opportunity_id=ANY($1::uuid[]) AND status='offered'""",offered)
            return {"kind":"none"}
        if operation == "operation_choice":
            operation_id=selected.get("operation_id")
            if not operation_id: return {"kind":"none"}
            from .cognitive_operations import CognitiveOperationEngine
            accepted=await CognitiveOperationEngine(self.db).accept_choice(UUID(str(operation_id)),selected)
            return {"kind":"cognitive_operation_choice","operation_id":str(operation_id),
                    "option_index":selected.get("option_index"),"accepted":accepted}
        oid=selected.get("opportunity_id")
        if not oid: return {"kind":"none"}
        opportunity=await self.db.fetchrow(
            "SELECT * FROM aios.character_cognitive_opportunity WHERE opportunity_id=$1 FOR UPDATE",UUID(str(oid)))
        if not opportunity or opportunity["status"] not in {"pending","offered"}:
            return {"kind":"stale_opportunity"}
        opportunity=dict(opportunity)
        if offered:
            await self.db.execute(
                """UPDATE aios.character_cognitive_opportunity
                   SET status=CASE WHEN opportunity_id=$2 THEN 'selected' ELSE 'suppressed' END,
                       selected_at=CASE WHEN opportunity_id=$2 THEN now() ELSE selected_at END,
                       resolved_at=CASE WHEN opportunity_id<>$2 THEN now() ELSE resolved_at END,
                       updated_at=now()
                   WHERE opportunity_id=ANY($1::uuid[]) AND status='offered'""",
                offered,UUID(str(oid)))
        from .cognitive_operations import CognitiveOperationEngine
        thread_id=selected.get("thread_id")
        operation_id=await CognitiveOperationEngine(self.db).create_from_opportunity(
            opportunity=opportunity,
            thread_id=UUID(str(thread_id)) if thread_id else None,
            priority=int(tx["priority"]), source_task_id=tx.get("source_task_id"),
            episode_id=tx.get("episode_id"),episode_role="attention")
        return {"kind":"cognitive_operation","operation_id":str(operation_id),
                "opportunity_id":str(oid),"operation_type":str(opportunity["operation_type"])}

    async def _timely(self, tx: Mapping[str,Any], selected: Mapping[str,Any]) -> tuple[bool,str]:
        now=datetime.now(timezone.utc)
        if tx["expires_at"] <= now: return False,"transaction expired"
        current=await self.huds.contexts.resolve(tx["instance_id"])
        operation=str(selected.get("operation") or "wait")
        # Read-only/background choices tolerate intervening state but never a
        # source-timeline switch. Immediate choices require the exact snapshot.
        if tx["source_timeline_id"] and current.source_timeline_id != tx["source_timeline_id"]:
            return False,"source timeline changed"
        policy=str(selected.get("freshness_policy") or ("strict" if operation=="urgent" else "contextual"))
        oid=selected.get("opportunity_id")
        if oid:
            row=await self.db.fetchrow(
                "SELECT freshness_policy,source_state_version,source_node_id,valid_until,status "
                "FROM aios.character_cognitive_opportunity WHERE opportunity_id=$1",UUID(str(oid)))
            if not row or row["status"] not in {"pending","offered"}:
                return False,"opportunity no longer live"
            if row["valid_until"] <= now:
                return False,"opportunity expired"
            policy=str(row["freshness_policy"] or policy)
            if policy == "strict" and (
                current.state_version != row["source_state_version"]
                or current.source_head_node_id != row["source_node_id"]
            ):
                return False,"strict opportunity context advanced"
        elif policy == "strict":
            if current.state_version != tx["source_state_version"] or current.source_head_node_id != tx["source_node_id"]:
                return False,"strict opportunity context advanced"
        return True,"current"

    async def _reject(self, transaction_id:UUID,status:str,reason:str,
                      choice:str|None=None,operation:str|None=None,focus:str|None=None)->TransactionResult:
        await self.db.execute(
            """UPDATE aios.internal_cognition_transaction SET status=$2,
                 rejection_reason=$3,completed_at=now(),updated_at=now()
               WHERE transaction_id=$1 AND status IN ('pending','running','proposed')""",
            transaction_id,status,reason[:1000],
        )
        await self.reconcile_terminal(transaction_id)
        return TransactionResult(transaction_id,status,choice,operation,focus)

    async def reconcile_terminal(self, transaction_id: UUID) -> None:
        episode_id = None
        async with self.db.atomic():
            row=await self.db.fetchrow(
                "SELECT * FROM aios.internal_cognition_transaction WHERE transaction_id=$1 FOR UPDATE",
                transaction_id)
            if not row or row["status"] not in {"consumed","stale","invalid","superseded","failed"}:
                return
            tx=dict(row)
            episode_id=tx.get("episode_id")
            if not tx.get("reconciled_at"):
                if tx["status"] != "consumed":
                    offered=list(tx.get("opportunity_ids") or [])
                    if offered:
                        await self.db.execute(
                            """UPDATE aios.character_cognitive_opportunity
                               SET status='suppressed',resolved_at=now(),updated_at=now()
                               WHERE opportunity_id=ANY($1::uuid[]) AND status='offered'""",offered)
                    candidate_ids=[UUID(str(c["operation_id"]))
                                   for c in self._json(tx.get("candidates"),[]) if c.get("operation_id")]
                    operations=await self.db.fetch(
                        """SELECT * FROM aios.character_cognitive_operation
                           WHERE (transaction_id=$1 OR operation_id=ANY($2::uuid[]))
                             AND status IN ('queued','running','waiting_inference')""",
                        transaction_id,candidate_ids)
                    from .cognitive_operations import CognitiveOperationEngine
                    engine=CognitiveOperationEngine(self.db)
                    for op in operations:
                        await engine._stale(dict(op),f"decision transaction {tx['status']}")
                    if tx.get("source_task_id"):
                        from .lifecycle import CharacterAgencyStore
                        agency=CharacterAgencyStore(self.db)
                        task=await agency.get_task(tx["source_task_id"])
                        if task and task.status in {"waiting","running","queued"}:
                            await agency.transition_task(task.task_id,"failed",error=f"attention transaction {tx['status']}")
                            from .runtime import AgentRuntimeStore
                            runtime=AgentRuntimeStore(self.db)
                            await runtime.finish_semantic_turn(task.instance_id,semantic=False)
                            if task.parent_task_id:
                                await runtime.wake(instance_id=task.instance_id,
                                    event_type="COGNITIVE_TASK_FAILED",source_type="cognitive_task",
                                    source_id=str(task.task_id),priority=max(1,task.priority-1),
                                    payload={"task_id":str(task.task_id),"parent_task_id":str(task.parent_task_id),
                                             "status":"failed","result":{"transaction_id":str(transaction_id)}},
                                    dedupe_key=f"task:{task.task_id}:failed")
                elif (not episode_id and not tx.get("source_task_id")
                      and tx.get("transaction_kind") == "route"):
                    result=self._json(tx.get("result"),{})
                    if result.get("operation") == "wait":
                        from .admission import AutonomyAdmissionService
                        await AutonomyAdmissionService(self.db).mark_episode_succeeded(
                            instance_id=tx["instance_id"],through_node_id=tx.get("source_node_id"))
                await self.db.execute(
                    "UPDATE aios.internal_cognition_transaction SET reconciled_at=now() WHERE transaction_id=$1",
                    transaction_id)
        if episode_id:
            from .episodes import CognitiveEpisodeStore
            await CognitiveEpisodeStore(self.db).reconcile(episode_id)

    @staticmethod
    def _json(value:Any,default:Any)->Any:
        if isinstance(value,(dict,list)): return value
        try: return json.loads(value) if value is not None else default
        except (TypeError,json.JSONDecodeError): return default
