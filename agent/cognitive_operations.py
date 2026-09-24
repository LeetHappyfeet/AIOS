from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.pipeline.jobs import enqueue_job
from aios_app.epistemic.research import CorpusSearchService


class CognitiveOperationEngine:
    """Execute one bounded cognitive operation; inference is queued, never hidden here."""

    def __init__(self, db: Database):
        self.db = db

    async def create_from_opportunity(self, *, opportunity: Mapping[str, Any],
                                      thread_id: UUID | None, priority: int = 100) -> UUID:
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.character_cognitive_operation(
                   thread_id,instance_id,opportunity_id,operation_type,input,
                   source_state_version,source_node_id,freshness_policy)
               VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8) RETURNING operation_id""",
            thread_id, opportunity["instance_id"], opportunity["opportunity_id"],
            opportunity["operation_type"],
            json.dumps(opportunity.get("operation_payload") or {}, default=str),
            opportunity.get("source_state_version"), opportunity.get("source_node_id"),
            opportunity.get("freshness_policy") or "contextual",
        )
        operation_id=row["operation_id"]
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
                result={"kind":"recall","input":self._json(op["input"],{})}
                await self._finish(op,result)
            elif kind=="corpus.search":
                payload=self._json(op["input"],{})
                found=await CorpusSearchService(self.db).search(
                    instance_id=op["instance_id"],query=str(payload.get("query") or ""),limit=8)
                await self._finish(op,{"kind":"inquiry","research_id":str(found.research_id),
                                       "status":found.status,"hits":found.reference_context()})
            elif kind in {"reflection.review","planning.review","executive.review"}:
                await self._queue_decision(op)
            else:
                await self._finish(op,{"kind":"unsupported","operation_type":kind})
        except Exception as exc:
            await self.db.execute(
                """UPDATE aios.character_cognitive_operation SET status='failed',
                   result=$2::jsonb,completed_at=now(),updated_at=now() WHERE operation_id=$1""",
                operation_id,json.dumps({"error":str(exc)[:1000]}))
            raise

    async def _queue_decision(self, op: Mapping[str,Any]) -> None:
        from aios_app.agent.transactions import InternalCognitionTransactions
        payload=self._json(op["input"],{})
        subject=" ".join(str(v) for v in payload.values() if v)[:220]
        labels={
            "reflection.review":[
                f"This seems important; keep it in mind: {subject}.",
                f"I may be reading too much into this: {subject}.",
                f"This changes how I understand the situation: {subject}.",
                "I do not have enough evidence to decide what it means."],
            "planning.review":[
                "My current goal still makes sense.",
                "I should reconsider the current goal before proceeding.",
                "This creates a new prerequisite or blocker.",
                "I do not need to change the plan yet."],
            "executive.review":[
                "This deserves immediate attention.",
                "I should wait for more information.",
                "I should preserve this concern but not act yet.",
                "This no longer needs action."],
        }[str(op["operation_type"])]
        candidates=[{"key":k,"label":label,"operation":"operation_choice",
                     "operation_id":str(op["operation_id"]),"option_index":i}
                    for i,(k,label) in enumerate(zip("ABCD",labels))]
        candidates.append({"key":"E","label":"Stop considering this for now.",
                           "operation":"operation_choice","operation_id":str(op["operation_id"]),
                           "option_index":4})
        tx=await InternalCognitionTransactions(self.db).create(
            instance_id=op["instance_id"],candidates=candidates,priority=120,ttl_seconds=300)
        await self.db.execute(
            """UPDATE aios.character_cognitive_operation SET status='waiting_inference',
               transaction_id=$2,updated_at=now() WHERE operation_id=$1""",
            op["operation_id"],tx)
        await enqueue_job(self.db,job_type="internal_cognition_inference",
            payload={"instance_id":str(op["instance_id"]),"transaction_id":str(tx),
                     "operation_id":str(op["operation_id"])},priority=120)

    async def accept_choice(self, operation_id: UUID, selected: Mapping[str,Any]) -> None:
        row=await self.db.fetchrow(
            "SELECT * FROM aios.character_cognitive_operation WHERE operation_id=$1",operation_id)
        if not row or row["status"]!="waiting_inference":
            return
        await self._finish(dict(row),{"kind":"choice","option_index":selected.get("option_index"),
                                      "label":selected.get("label")})

    async def _finish(self, op: Mapping[str,Any], result: Mapping[str,Any]) -> None:
        await self.db.execute(
            """UPDATE aios.character_cognitive_operation SET status='succeeded',result=$2::jsonb,
               completed_at=now(),updated_at=now() WHERE operation_id=$1""",
            op["operation_id"],json.dumps(result,default=str))
        if op.get("opportunity_id"):
            await self.db.execute(
                """UPDATE aios.character_cognitive_opportunity SET status='executed',
                   resolved_at=now(),updated_at=now() WHERE opportunity_id=$1""",op["opportunity_id"])
        if op.get("thread_id"):
            await self.db.execute(
                """UPDATE aios.character_cognitive_thread SET status='open',
                   pressure=GREATEST(0,pressure-1),meta=meta || $2::jsonb,updated_at=now()
                   WHERE thread_id=$1""",op["thread_id"],
                json.dumps({"last_result":result},default=str))

    @staticmethod
    def _json(value:Any,default:Any)->Any:
        if isinstance(value,(dict,list)): return value
        try: return json.loads(value) if value is not None else default
        except (TypeError,json.JSONDecodeError): return default
