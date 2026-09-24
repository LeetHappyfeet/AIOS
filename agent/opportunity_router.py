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

    async def admit(self, *, instance_id:UUID, source_node_id:UUID|None=None) -> UUID|None:
        batch=await self.opportunities.generate(
            instance_id=instance_id,source_node_id=source_node_id,limit=8)
        rows=[x for x in batch.opportunities
              if x["status"]=="pending" and x["valid_until"] is not None]
        if not rows: return None
        # Avoid asking the character to choose among duplicate cognitive modes.
        selected=[]; seen=set()
        for row in sorted(rows,key=lambda x:float(x["priority_score"]),reverse=True):
            key=str(row["opportunity_type"])
            if key in seen: continue
            seen.add(key); selected.append(row)
            if len(selected)>=4: break
        if not selected: return None
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
        priority=max(1,200-int(max(float(x["priority_score"]) for x in selected)*20))
        tx=await self.transactions.create(
            instance_id=instance_id,candidates=candidates,priority=priority,ttl_seconds=300,
            opportunity_ids=[x["opportunity_id"] for x in selected])
        await self.db.execute(
            """UPDATE aios.character_cognitive_opportunity SET status='offered',updated_at=now()
               WHERE opportunity_id=ANY($1::uuid[]) AND status='pending'""",
            [x["opportunity_id"] for x in selected])
        await enqueue_job(self.db,job_type="internal_cognition_inference",
            payload={"instance_id":str(instance_id),"transaction_id":str(tx)},priority=priority)
        return tx
