from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from .lifecycle import CognitiveTask
from .opportunities import CognitiveOpportunityService


class TaskOpportunityPlanner:
    """Turn a durable task into host-owned opportunities.

    The task objective influences labels/search focus, never executable syntax.
    Operation payloads are still constructed and validated by AIOS.
    """

    def __init__(self, db):
        self.db=db
        self.opportunities=CognitiveOpportunityService(db)

    async def generate(self, task: CognitiveTask, *, limit: int = 5) -> list[dict[str,Any]]:
        focus=(task.retrieval_focus or task.objective).strip()
        batch=await self.opportunities.generate(
            instance_id=task.instance_id,
            source_node_id=task.source_through_node_id or task.source_node_id,
            focus_override=focus,
            limit=max(limit,8),
        )
        rows=list(batch.opportunities)\n        allowed={
            "research":{"knowledge_gap","memory_recall","reflection"},
            "planning":{"goal_review","knowledge_gap","memory_recall","reflection"},
            "reflection":{"memory_recall","reflection","knowledge_gap"},
            "executive":{"immediate","goal_review","memory_recall","knowledge_gap","reflection"},
            "communication":{"memory_recall","reflection"},
        }.get(task.task_type,set())
        ranked=[r for r in rows if str(r.get("opportunity_type")) in allowed]
        # Keep the durable task visible in the human-readable choice without
        # allowing inference to author operation arguments.
        for row in ranked:
            row["natural_language"]=(
                f"For task '{task.objective[:140]}': {row.get('natural_language') or ''}"
            )[:260]
        return ranked[:max(1,min(limit,5))]

    async def dependencies(self, task_id: UUID) -> list[dict[str,Any]]:
        rows=await self.db.fetch(
            """SELECT d.child_task_id,d.status,d.result,c.task_type,c.objective
               FROM aios.character_cognitive_task_dependency d
               JOIN aios.character_cognitive_task c ON c.task_id=d.child_task_id
               WHERE d.parent_task_id=$1 ORDER BY d.created_at""",task_id)
        return [dict(r) for r in rows]
