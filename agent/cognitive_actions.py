from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.hud.context import HUDContextResolver
from aios_app.epistemic.cognitive_context import CognitiveContextService
from aios_app.epistemic.relevance import CognitiveRelevanceScorer
from .actions import ActionRegistry, ActionSpec
from .lifecycle import CharacterAgencyStore


def register_cognitive_actions(db: Database, registry: ActionRegistry) -> None:
    agency = CharacterAgencyStore(db)
    contexts = HUDContextResolver(db)
    cognition = CognitiveContextService(db)

    async def memory_search(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        query = str(args["query"]).strip()
        context = await contexts.resolve(instance_id)
        raw = await db.fetchrow("SELECT * FROM aios.character_runtime_state WHERE instance_id=$1", instance_id)
        attention = await cognition.resolve_attention_inputs(
            context, dict(raw or {}), {}, recent_limit=12, focus_text=query,
        )
        scorer = CognitiveRelevanceScorer(context, focus_text=query, goals=attention.goals)
        snapshot = await cognition.resolve_knowledge(context, scorer, attention)
        limit = max(1, min(int(args.get("limit", 8)), 20))
        candidates = list(snapshot.recalled_memories) + list(snapshot.beliefs) + list(snapshot.goals)
        return {"query": query, "matches": [
            {
                "text": item.get("text"), "claim_kind": item.get("claim_kind"),
                "proposition_id": str(item.get("proposition_id") or ""),
                "source_node_id": str(item.get("source_node_id") or ""),
                "confidence": item.get("effective_confidence", item.get("confidence")),
            }
            for item in candidates[:limit]
        ]}

    async def goal_create(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        row = await db.execute_returning_row(
            """INSERT INTO aios.character_agent_goal(instance_id,goal_text,priority,meta)
               VALUES($1,$2,$3,$4::jsonb) RETURNING goal_id,status""",
            instance_id, str(args["goal"]).strip(), int(args.get("priority",100)),
            json.dumps({"created_by":"cognitive_action"}),
        )
        return {"goal_id":str(row["goal_id"]),"status":row["status"],"goal":str(args["goal"])}

    async def goal_update(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        goal_id=UUID(str(args["goal_id"]))
        row=await db.execute_returning_row(
            """UPDATE aios.character_agent_goal SET goal_text=COALESCE($3,goal_text),
               priority=COALESCE($4,priority),updated_at=now()
               WHERE goal_id=$1 AND instance_id=$2 RETURNING goal_id,goal_text,status,priority""",
            goal_id,instance_id,args.get("goal"),args.get("priority"),
        )
        if not row: raise LookupError("goal not found")
        return dict(row)

    async def goal_finish(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        goal_id=UUID(str(args["goal_id"])); status=str(args.get("status","completed"))
        if status not in {"completed","cancelled"}: raise ValueError("invalid terminal goal status")
        row=await db.execute_returning_row(
            """UPDATE aios.character_agent_goal SET status=$3,completed_at=now(),updated_at=now()
               WHERE goal_id=$1 AND instance_id=$2 AND status='active' RETURNING goal_id,status""",
            goal_id,instance_id,status,
        )
        if not row: raise LookupError("active goal not found")
        return {"goal_id":str(row["goal_id"]),"status":row["status"]}

    async def task_create(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        task_type=str(args.get("task_type") or "executive")
        task=await agency.create_task(
            instance_id=instance_id, task_type=task_type, objective=str(args["objective"]),
            retrieval_focus=args.get("retrieval_focus"), priority=int(args.get("priority",100)),
            trigger_type="cognitive_delegation", execution_mode=str(args.get("execution_mode","auto")),
            meta={"created_by":"cognitive_action"},
        )
        return {"task_id":str(task.task_id),"task_type":task.task_type,"status":task.status}

    async def task_defer(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        task_id=UUID(str(args["task_id"]))
        task=await agency.get_task(task_id)
        if not task or task.instance_id != instance_id: raise LookupError("task not found")
        if task.status == "running": task=await agency.transition_task(task_id,"waiting")
        return {"task_id":str(task.task_id),"status":task.status}

    async def delegate(kind: str, instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        task=await agency.create_task(
            instance_id=instance_id, task_type=kind, objective=str(args["objective"]),
            retrieval_focus=args.get("focus"), priority=int(args.get("priority",100)),
            trigger_type="cognitive_delegation", execution_mode="auto",
            meta={"created_by":"cognitive_action","specialization":kind},
        )
        return {"task_id":str(task.task_id),"task_type":kind,"status":task.status}

    registry.register(ActionSpec("memory.search",{"type":"object","required":["query"],"properties":{"query":{"type":"string"},"limit":{"type":"integer"}},"additionalProperties":False},"read_only",frozenset({"executive","research","planning","reflection"}),memory_search,"return_to_cognition"))
    registry.register(ActionSpec("goal.create",{"type":"object","required":["goal"],"properties":{"goal":{"type":"string"},"priority":{"type":"integer"}},"additionalProperties":False},"internal_write",frozenset({"executive","planning","reflection"}),goal_create))
    registry.register(ActionSpec("goal.update",{"type":"object","required":["goal_id"],"properties":{"goal_id":{"type":"string"},"goal":{"type":"string"},"priority":{"type":"integer"}},"additionalProperties":False},"internal_write",frozenset({"executive","planning","reflection"}),goal_update))
    registry.register(ActionSpec("goal.complete",{"type":"object","required":["goal_id"],"properties":{"goal_id":{"type":"string"},"status":{"type":"string"}},"additionalProperties":False},"internal_write",frozenset({"executive","planning"}),goal_finish))
    registry.register(ActionSpec("task.create",{"type":"object","required":["objective"],"properties":{"objective":{"type":"string"},"task_type":{"type":"string"},"retrieval_focus":{"type":"string"},"priority":{"type":"integer"},"execution_mode":{"type":"string"}},"additionalProperties":False},"internal_write",frozenset({"executive","planning"}),task_create,"asynchronous"))
    registry.register(ActionSpec("task.defer",{"type":"object","required":["task_id"],"properties":{"task_id":{"type":"string"}},"additionalProperties":False},"internal_write",frozenset({"executive","planning"}),task_defer))
    for kind in ("research","planning","reflection"):
        async def handler(instance_id: UUID, args: Mapping[str, Any], _kind=kind):
            return await delegate(_kind,instance_id,args)
        registry.register(ActionSpec(f"{kind}.request",{"type":"object","required":["objective"],"properties":{"objective":{"type":"string"},"focus":{"type":"string"},"priority":{"type":"integer"}},"additionalProperties":False},"internal_write",frozenset({"executive","planning","reflection"}),handler,"asynchronous"))
