from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from aios_app.db import Database
from aios_app.hud.internal_frame import InternalHUDAssembler
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
    ) -> UUID:
        choices = [dict(x) for x in candidates][:5]
        hud = await self.huds.build(instance_id, clues=clues, candidates=choices)
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.internal_cognition_transaction(
                 instance_id,source_task_id,priority,source_state_version,
                 source_timeline_id,source_node_id,context_fingerprint,clues,
                 candidates,prompt_text,prompt_hash,expires_at,opportunity_ids)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,
                      now()+($12*interval '1 second'),$13::uuid[])
               RETURNING transaction_id""",
            instance_id, source_task_id, int(priority), hud.state_version,
            hud.source_timeline_id, hud.source_node_id, hud.fingerprint,
            json.dumps(list(clues)[:2]), json.dumps(choices), hud.prompt,
            hashlib.sha256(hud.prompt.encode()).hexdigest(),
            max(10,min(int(ttl_seconds),3600)), list(opportunity_ids),
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
            await self.db.execute(
                """UPDATE aios.internal_cognition_transaction
                   SET status='stale',rejection_reason='expired before claim',updated_at=now()
                   WHERE transaction_id=$1 AND status='pending'""", transaction_id,
            )
            return TransactionResult(transaction_id,"stale",None,None,None)

        tx=dict(row); candidates=self._json(tx["candidates"],[])
        output_schema={"type":"object","required":["choice"],"properties":{
            "choice":{"type":"string"}},"additionalProperties":False}
        try:
            inference=await self.broker.infer(InferenceRequest(
                instance_id=tx["instance_id"], task_id=tx["source_task_id"],
                worker_class="executive", prompt=tx["prompt_text"],
                context_state_version=tx["source_state_version"],
                hud_profile_name=tx["hud_profile_name"],
                allowed_actions={}, output_schema=output_schema,
            ))
            raw=inference.response.raw
            choice=str(raw.get("choice") or "").strip().upper()
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
            materialized=await self._materialize(tx,selected,focus)
            result={"operation":selected.get("operation"),"focus":focus,"choice":choice,
                    "materialized":materialized}
            await self.db.execute(
                """UPDATE aios.internal_cognition_transaction SET status='consumed',
                     result=$2::jsonb,completed_at=now(),consumed_at=now(),updated_at=now()
                   WHERE transaction_id=$1 AND status='proposed'""",
                transaction_id,json.dumps(result),
            )
            return TransactionResult(transaction_id,"consumed",choice,str(selected.get("operation")),focus)
        except Exception as exc:
            await self.db.execute(
                """UPDATE aios.internal_cognition_transaction SET status='failed',
                     rejection_reason=$2,completed_at=now(),updated_at=now()
                   WHERE transaction_id=$1 AND status IN ('running','proposed')""",
                transaction_id,str(exc)[:1000],
            )
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
            await CognitiveOperationEngine(self.db).accept_choice(UUID(str(operation_id)),selected)
            return {"kind":"cognitive_operation_choice","operation_id":str(operation_id),
                    "option_index":selected.get("option_index")}
        oid=selected.get("opportunity_id")
        if not oid: return {"kind":"none"}
        opportunity=await self.db.fetchrow(
            "SELECT * FROM aios.character_cognitive_opportunity WHERE opportunity_id=$1",UUID(str(oid)))
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
            priority=int(tx["priority"]))
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
               WHERE transaction_id=$1 AND status IN ('running','proposed')""",
            transaction_id,status,reason[:1000],
        )
        return TransactionResult(transaction_id,status,choice,operation,focus)

    @staticmethod
    def _json(value:Any,default:Any)->Any:
        if isinstance(value,(dict,list)): return value
        try: return json.loads(value) if value is not None else default
        except (TypeError,json.JSONDecodeError): return default
