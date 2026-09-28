from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from aios_app.db import Database
from .opportunities import CognitiveOpportunityService
from .transactions import InternalCognitionTransactions
from .threads import CognitiveThreadService
from aios_app.pipeline.jobs import enqueue_job


class OpportunityRouter:
    """Turn graph-derived opportunities into one disposable character decision."""

    def __init__(self,db:Database):
        self.db=db
        self.opportunities=CognitiveOpportunityService(db)
        self.transactions=InternalCognitionTransactions(db)
        self.threads=CognitiveThreadService(db)

    async def admit(self, *, instance_id:UUID, source_node_id:UUID|None=None,\n                    source_task_id:UUID|None=None, enqueue_inference:bool=True) -> UUID|None:\n        batch=await self.opportunities.generate(
            instance_id=instance_id,source_node_id=source_node_id,limit=8)
        rows=[x for x in batch.opportunities
              if x["status"]=="pending" and x["valid_until"] is not None]
        if not rows: return None
        ranked=sorted(rows,key=lambda x:float(x["priority_score"]),reverse=True)
        # Reserve one due goal review for lifecycle maintenance. It is removed
        # from the competitive attention transaction so the same opportunity
        # can never be selected twice.
        goal_review=next(
            (row for row in ranked if str(row["opportunity_type"])=="goal_review"),None)
        attention_rows=[row for row in ranked if row is not goal_review]
        # Avoid asking the character to choose among duplicate cognitive modes.
        selected=[]; seen=set()
        for row in attention_rows:
            key=str(row["opportunity_type"])
            if key in seen: continue
            seen.add(key); selected.append(row)
            if len(selected)>=4: break
        if not selected and goal_review is None:
            return None
        keys="ABCD"
        candidates=[]
        for key,row in zip(keys,selected):
            thread_id=await self.threads.cross(row)
            candidates.append({
                "key":key,"label":row["natural_language"],
                "operation":"opportunity","opportunity_id":str(row["opportunity_id"]),
                "thread_id":str(thread_id),
                "freshness_policy":row["freshness_policy"],
            })
        candidates.append({"key":"E","label":"None of these deserves my attention right now.",
                           "operation":"wait"})
        scored=selected + ([goal_review] if goal_review is not None else [])
        priority=max(1,200-int(max(float(x["priority_score"]) for x in scored)*20))
        tx=await self.transactions.create(
            instance_id=instance_id,candidates=candidates,priority=priority,ttl_seconds=300,
            opportunity_ids=[x["opportunity_id"] for x in selected],\n            source_task_id=source_task_id)
        await self.db.execute(
            """UPDATE aios.character_cognitive_opportunity SET status='offered',updated_at=now()
               WHERE opportunity_id=ANY($1::uuid[]) AND status='pending'""",
            [x["opportunity_id"] for x in selected])
        if enqueue_inference:
            await enqueue_job(self.db,job_type="internal_cognition_inference",
                payload={"instance_id":str(instance_id),"transaction_id":str(tx)},priority=priority)

        # Goal lifecycle maintenance has one reserved slot per admitted host
        # cognition episode. It does not compete with the attention choice and
        # never creates cognition on its own: this path only runs because a
        # COGNITIVE_DELTA_READY episode was already admitted.
        if goal_review is not None:
            maintenance_thread=await self.threads.cross(goal_review)
            from .cognitive_operations import CognitiveOperationEngine
            await CognitiveOperationEngine(self.db).create_from_opportunity(
                opportunity=goal_review,thread_id=maintenance_thread,priority=priority)
            await self.db.execute(
                """UPDATE aios.character_cognitive_opportunity
                   SET status='selected',selected_at=COALESCE(selected_at,now()),updated_at=now()
                   WHERE opportunity_id=$1 AND status IN ('pending','offered')""",
                goal_review["opportunity_id"])
        return tx
