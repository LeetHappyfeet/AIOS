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
    ) -> UUID:
        choices = [dict(x) for x in candidates][:5]
        hud = await self.huds.build(instance_id, clues=clues, candidates=choices)
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.internal_cognition_transaction(
                 instance_id,source_task_id,priority,source_state_version,
                 source_timeline_id,source_node_id,context_fingerprint,clues,
                 candidates,prompt_text,prompt_hash,expires_at)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,
                      now()+($12*interval '1 second'))
               RETURNING transaction_id""",
            instance_id, source_task_id, int(priority), hud.state_version,
            hud.source_timeline_id, hud.source_node_id, hud.fingerprint,
            json.dumps(list(clues)[:2]), json.dumps(choices), hud.prompt,
            hashlib.sha256(hud.prompt.encode()).hexdigest(),
            max(10,min(int(ttl_seconds),3600)),
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
            "choice":{"type":"string"},"focus":{"type":"string"}},"additionalProperties":False}
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
            focus=" ".join(str(raw.get("focus") or "").split())[:240] or None
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
            result={"operation":selected.get("operation"),"focus":focus,"choice":choice}
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

    async def _timely(self, tx: Mapping[str,Any], selected: Mapping[str,Any]) -> tuple[bool,str]:
        now=datetime.now(timezone.utc)
        if tx["expires_at"] <= now: return False,"transaction expired"
        current=await self.huds.contexts.resolve(tx["instance_id"])
        operation=str(selected.get("operation") or "wait")
        # Read-only/background choices tolerate intervening state but never a
        # source-timeline switch. Immediate choices require the exact snapshot.
        if tx["source_timeline_id"] and current.source_timeline_id != tx["source_timeline_id"]:
            return False,"source timeline changed"
        if operation == "urgent":
            if current.state_version != tx["source_state_version"] or current.source_head_node_id != tx["source_node_id"]:
                return False,"immediate context advanced"
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
