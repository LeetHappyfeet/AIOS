from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService
from aios_app.epistemic.scene_state import CharacterSceneStateStore
from aios_app.hud.context import HUDContextResolver


@dataclass(frozen=True)
class ResolvedScene:
    location: dict[str, Any] | None
    present_entities: tuple[dict[str, Any], ...]
    immediate_goal: dict[str, Any] | None
    pending_work: dict[str, Any] | None
    last_significant_change: dict[str, Any] | None
    evidence_node_ids: tuple[UUID, ...]


class CharacterSceneResolver:
    """Resolve scene slots from their durable authorities.

    This is projection logic, not HUD presentation logic. Objective location and
    co-location come from /world projection; intention comes from managed goals;
    pending work comes from durable task/action lifecycle.
    """

    def __init__(self, db: Database):
        self.db=db
        self.contexts=HUDContextResolver(db)
        self.goals=CharacterGoalService(db)

    async def resolve(self, instance_id: UUID) -> tuple[Any,ResolvedScene]:
        context=await self.contexts.resolve(instance_id)
        location=None
        if context.location_entity_id:
            row=await self.db.fetchrow(
                """SELECT entity_id,entity_type,entity_key,display_name
                   FROM aios.world_entity WHERE world_id=$1 AND entity_id=$2""",
                context.world_id,context.location_entity_id)
            if row:
                location={**dict(row),"source":"world.location"}

        rows=await self.db.fetch(
            """SELECT e.entity_id,e.entity_type,e.entity_key,e.display_name
               FROM aios.world_entity_relation r
               JOIN aios.world_entity e ON e.entity_id=r.subject_entity_id
               WHERE r.world_id=$1 AND r.relation_type='located_in'
                 AND r.object_entity_id=$2 AND r.valid_to_node_id IS NULL
                 AND e.entity_id<>$3
               ORDER BY e.created_at""",
            context.world_id,context.location_entity_id,context.entity_id
        ) if context.location_entity_id else []
        present=tuple({**dict(row),"basis":"co_located"} for row in rows)

        active=await self.goals.resolve_active(instance_id=instance_id)
        goal=active.immediate
        immediate=goal.hud_item() if goal else None

        action=await self.db.fetchrow(
            """SELECT action_id,task_id,action_type,status,arguments,updated_at
               FROM aios.character_action
               WHERE instance_id=$1 AND status=ANY($2::text[])
               ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'waiting' THEN 1
                                    WHEN 'validated' THEN 2 ELSE 3 END,
                        updated_at DESC LIMIT 1""",
            instance_id,["running","waiting","validated","queued","proposed"])
        pending=None
        if action:
            pending={"kind":"action","action_id":str(action["action_id"]),
                     "task_id":str(action["task_id"]) if action["task_id"] else None,
                     "action_type":str(action["action_type"]),"status":str(action["status"])}
        else:
            task=await self.db.fetchrow(
                """SELECT task_id,task_type,objective,status,updated_at
                   FROM aios.character_cognitive_task
                   WHERE instance_id=$1 AND status=ANY($2::text[])
                   ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,
                            priority,updated_at DESC LIMIT 1""",
                instance_id,["running","waiting","queued"])
            if task:
                pending={"kind":"task","task_id":str(task["task_id"]),
                         "task_type":str(task["task_type"]),"status":str(task["status"]),
                         "text":str(task["objective"])}

        transition=await self.db.fetchrow(
            """SELECT source_node_id,after_value,created_at
               FROM aios.character_scene_transition
               WHERE instance_id=$1 AND slot_key='last_significant_change'
                 AND status='active'
               ORDER BY created_at DESC LIMIT 1""",instance_id)
        last=None
        if transition:
            value=transition["after_value"]
            last=value if isinstance(value,dict) else {"text":str(value)}
            if transition["source_node_id"]:
                last={**last,"source_node_id":str(transition["source_node_id"])}

        evidence=tuple(dict.fromkeys(v for v in
            (context.source_head_node_id,context.head_node_id) if v))
        return context,ResolvedScene(location,present,immediate,pending,last,evidence)


class CharacterSceneProjector:
    def __init__(self,db:Database):
        self.db=db
        self.resolver=CharacterSceneResolver(db)
        self.store=CharacterSceneStateStore(db)

    async def refresh(self,instance_id:UUID)->dict[str,Any]:
        context,scene=await self.resolver.resolve(instance_id)
        return await self.store.materialize(
            instance_id=instance_id,runtime_timeline_id=context.timeline_id,
            runtime_head_node_id=context.head_node_id,
            source_timeline_id=context.source_timeline_id,
            source_head_node_id=context.source_head_node_id,
            scene={"location":scene.location,"present_entities":scene.present_entities,
                   "immediate_goal":scene.immediate_goal,"pending_work":scene.pending_work,
                   "last_significant_change":scene.last_significant_change},
            evidence_node_ids=scene.evidence_node_ids)
