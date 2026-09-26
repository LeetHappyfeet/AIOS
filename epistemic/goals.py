from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database


@dataclass(frozen=True)
class CognitiveGoal:
    """Normalized active intention consumed by cognition and HUD surfaces."""

    goal_id: UUID | None
    text: str
    priority: int = 100
    source: str = "agent_goal"
    status: str = "active"
    meta: Mapping[str, Any] | None = None

    def __str__(self) -> str:
        return self.text

    def hud_item(self) -> dict[str, Any]:
        return {
            "goal_id": str(self.goal_id) if self.goal_id else None,
            "text": self.text,
            "priority": self.priority,
            "source": self.source,
            "status": self.status,
            "tier": 0,
        }


@dataclass(frozen=True)
class ResolvedGoalSet:
    active: tuple[CognitiveGoal, ...]

    @property
    def immediate(self) -> CognitiveGoal | None:
        return self.active[0] if self.active else None


class CharacterGoalService:
    """Single authority for current character intentions.

    Semantic GOAL propositions remain historical/epistemic evidence. This
    service owns the actionable lifecycle represented by character_agent_goal.
    Legacy character_runtime_state.goals are imported once, then cease to be
    an independent cognition authority.
    """

    def __init__(self, db: Any):
        # Database and an acquired asyncpg connection both satisfy the small
        # query surface used here. Supporting the latter lets goal admission
        # share the message-cognition transaction atomically.
        self.db = db

    async def _returning(self, sql: str, *args: Any) -> Any:
        helper = getattr(self.db, "execute_returning_row", None)
        if helper is not None:
            return await helper(sql, *args)
        return await self.db.fetchrow(sql, *args)

    async def resolve_active(
        self,
        instance_id: UUID,
        *,
        legacy_goals: Any = None,
    ) -> ResolvedGoalSet:
        await self._import_legacy(instance_id, legacy_goals)
        rows = await self.db.fetch(
            """SELECT goal_id,goal_text,status,priority,meta
               FROM aios.character_agent_goal
               WHERE instance_id=$1 AND status='active'
               ORDER BY priority ASC, created_at ASC, goal_id ASC""",
            instance_id,
        )
        goals = tuple(
            CognitiveGoal(
                goal_id=row["goal_id"],
                text=str(row["goal_text"]).strip(),
                priority=int(row["priority"]),
                source="agent_goal",
                status=str(row["status"]),
                meta=self._json_object(row.get("meta") if hasattr(row, "get") else row["meta"]),
            )
            for row in rows
            if str(row["goal_text"]).strip()
        )
        return ResolvedGoalSet(goals)

    async def create(
        self,
        *,
        instance_id: UUID,
        text: str,
        priority: int = 100,
        source_task_id: UUID | None = None,
        source_node_id: UUID | None = None,
        root_task_id: UUID | None = None,
        source_action_id: UUID | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> CognitiveGoal:
        clean = " ".join(str(text).split())
        if not clean:
            raise ValueError("goal text cannot be empty")
        row = await self._returning(
            """INSERT INTO aios.character_agent_goal(
                   instance_id,source_task_id,source_node_id,root_task_id,source_action_id,
                   goal_text,priority,meta)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
               RETURNING goal_id,goal_text,status,priority,meta""",
            instance_id, source_task_id, source_node_id, root_task_id, source_action_id,
            clean, int(priority), json.dumps(dict(meta or {})),
        )
        return self._goal(row)

    async def update(
        self,
        *,
        instance_id: UUID,
        goal_id: UUID,
        text: str | None = None,
        priority: int | None = None,
    ) -> CognitiveGoal:
        clean = None if text is None else " ".join(str(text).split())
        if clean == "":
            raise ValueError("goal text cannot be empty")
        row = await self._returning(
            """UPDATE aios.character_agent_goal
               SET goal_text=COALESCE($3,goal_text),
                   priority=COALESCE($4,priority),updated_at=now()
               WHERE goal_id=$1 AND instance_id=$2
               RETURNING goal_id,goal_text,status,priority,meta""",
            goal_id, instance_id, clean, priority,
        )
        if not row:
            raise LookupError("goal not found")
        return self._goal(row)

    async def finish(
        self,
        *,
        instance_id: UUID,
        goal_id: UUID,
        status: str = "completed",
    ) -> CognitiveGoal:
        if status not in {"completed", "cancelled"}:
            raise ValueError("invalid terminal goal status")
        row = await self._returning(
            """UPDATE aios.character_agent_goal
               SET status=$3,completed_at=now(),updated_at=now()
               WHERE goal_id=$1 AND instance_id=$2 AND status='active'
               RETURNING goal_id,goal_text,status,priority,meta""",
            goal_id, instance_id, status,
        )
        if not row:
            raise LookupError("active goal not found")
        return self._goal(row)

    async def _import_legacy(self, instance_id: UUID, raw: Any) -> None:
        values = self._json_list(raw)
        if not values:
            return
        for value in values:
            text = self._legacy_text(value)
            if not text:
                continue
            # Compatibility import is deliberately idempotent. It only bridges
            # old runtime state; it never reactivates a terminal managed goal.
            exists = await self.db.fetchrow(
                """SELECT goal_id FROM aios.character_agent_goal
                   WHERE instance_id=$1
                     AND lower(regexp_replace(goal_text,'\\s+',' ','g'))=lower($2)
                   LIMIT 1""",
                instance_id, text,
            )
            if exists:
                continue
            await self.create(
                instance_id=instance_id,
                text=text,
                priority=100,
                meta={"created_by": "legacy_runtime_goal_import"},
            )

    @staticmethod
    def _legacy_text(value: Any) -> str:
        if isinstance(value, Mapping):
            value = value.get("text") or value.get("goal") or ""
        return " ".join(str(value or "").split())

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return []
            return decoded if isinstance(decoded, list) else []
        return []

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return decoded if isinstance(decoded, dict) else {}
        return {}

    def _goal(self, row: Any) -> CognitiveGoal:
        return CognitiveGoal(
            goal_id=row["goal_id"],
            text=str(row["goal_text"]),
            priority=int(row["priority"]),
            source="agent_goal",
            status=str(row["status"]),
            meta=self._json_object(row.get("meta") if hasattr(row, "get") else row["meta"]),
        )
