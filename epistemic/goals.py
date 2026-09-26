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

    async def cognitive_states(
        self, instance_id: UUID, goals: tuple[CognitiveGoal, ...] | list[CognitiveGoal]
    ) -> dict[UUID, dict[str, Any]]:
        """Bounded deterministic lifecycle projection for active goals."""
        ids = [goal.goal_id for goal in goals if goal.goal_id is not None]
        if not ids:
            return {}
        evidence = await self.db.fetch(
            """SELECT goal_id,relation,count(*) AS count,
                      max(created_at) AS latest_at
               FROM aios.character_goal_evidence
               WHERE instance_id=$1 AND goal_id=ANY($2::uuid[])
               GROUP BY goal_id,relation""",
            instance_id, ids,
        )
        threads = await self.db.fetch(
            """SELECT goal_id,status,pressure,crossing_count,current_question,updated_at
               FROM aios.character_cognitive_thread
               WHERE instance_id=$1 AND goal_id=ANY($2::uuid[])
               ORDER BY goal_id,updated_at DESC""",
            instance_id, ids,
        )
        latest = await self.db.fetch(
            """SELECT DISTINCT ON (goal_id) goal_id,relation,evidence_type,confidence,meta
               FROM aios.character_goal_evidence
               WHERE instance_id=$1 AND goal_id=ANY($2::uuid[])
               ORDER BY goal_id,created_at DESC,evidence_id DESC NULLS LAST""",
            instance_id, ids,
        )
        out = {goal_id: {
            "thread_status": None, "pressure": 0, "crossing_count": 0,
            "progress_count": 0, "blocker_count": 0,
            "completion_candidate_count": 0, "contradiction_count": 0,
            "withdrawal_count": 0, "latest_evidence": None,
        } for goal_id in ids}
        for row in evidence:
            state = out.get(row["goal_id"])
            if state is not None:
                key = f"{str(row['relation'])}_count"
                if key in state:
                    state[key] = int(row["count"] or 0)
        seen: set[UUID] = set()
        for row in threads:
            goal_id = row["goal_id"]
            if goal_id in seen or goal_id not in out:
                continue
            seen.add(goal_id)
            out[goal_id].update({
                "thread_status": str(row["status"]),
                "pressure": int(row["pressure"] or 0),
                "crossing_count": int(row["crossing_count"] or 0),
                "current_question": row["current_question"],
            })
        for row in latest:
            if row["goal_id"] in out:
                out[row["goal_id"]]["latest_evidence"] = {
                    "relation": str(row["relation"]),
                    "evidence_type": str(row["evidence_type"]),
                    "confidence": float(row["confidence"] or 0),
                    "meta": self._json_object(row["meta"]),
                }
        return out

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
        goal = self._goal(row)
        await self._invalidate(instance_id)
        await self._refresh_scene(instance_id)
        return goal

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
        goal = self._goal(row)
        await self._invalidate(instance_id)
        await self._refresh_scene(instance_id)
        return goal

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
        goal = self._goal(row)
        await self._reconcile_terminal_threads(instance_id, goal_id, status)
        await self._invalidate(instance_id)
        await self._refresh_scene(instance_id)
        return goal

    async def reconcile_evidence(
        self,
        *,
        instance_id: UUID,
        text: str,
        topic_key: str,
        polarity: int,
        source_node_id: UUID | None,
        source_unit_id: UUID | None = None,
        confidence: float | None = None,
        salience: float | None = None,
    ) -> CognitiveGoal | None:
        """Project character-owned GOAL evidence into managed intention state.

        topic_key is the stable bridge between epistemic evidence and executive
        state. Positive evidence creates/refreshes one active managed goal.
        Negative evidence cancels the active goal for that topic. Historical
        rows are retained and are never silently reactivated.
        """
        topic = str(topic_key or "").strip()
        clean = " ".join(str(text or "").split())
        if not topic or not clean:
            return None
        rows = await self.db.fetch(
            """SELECT goal_id,goal_text,status,priority,meta
               FROM aios.character_agent_goal
               WHERE instance_id=$1
                 AND meta->>'semantic_topic_key'=$2
               ORDER BY created_at DESC, goal_id DESC""",
            instance_id, topic,
        )
        active = next((row for row in rows if str(row["status"]) == "active"), None)
        evidence_meta = {
            "semantic_topic_key": topic,
            "source": "message_cognition",
            "source_unit_id": str(source_unit_id) if source_unit_id else None,
            "confidence": confidence,
            "salience": salience,
        }
        if polarity < 0:
            if active:
                row = await self._returning(
                    """UPDATE aios.character_agent_goal
                       SET status='cancelled',completed_at=now(),updated_at=now(),
                           meta=meta || $3::jsonb
                       WHERE goal_id=$1 AND instance_id=$2 AND status='active'
                       RETURNING goal_id,goal_text,status,priority,meta""",
                    active["goal_id"], instance_id,
                    json.dumps({**evidence_meta, "resolution_kind": "negated"}),
                )
                if not row:
                    return None
                goal = self._goal(row)
                await self._reconcile_terminal_threads(
                    instance_id, goal.goal_id, "cancelled", resolution_kind="negated"
                )
                await self._invalidate(instance_id)
        await self._refresh_scene(instance_id)
                return goal
            return None

        if active:
            row = await self._returning(
                """UPDATE aios.character_agent_goal
                   SET goal_text=$3,source_node_id=COALESCE($4,source_node_id),
                       updated_at=now(),meta=meta || $5::jsonb
                   WHERE goal_id=$1 AND instance_id=$2
                   RETURNING goal_id,goal_text,status,priority,meta""",
                active["goal_id"], instance_id, clean, source_node_id,
                json.dumps(evidence_meta),
            )
            goal = self._goal(row)
            await self._invalidate(instance_id)
        await self._refresh_scene(instance_id)
            return goal

        # A terminal row means this topic already had an explicit lifecycle.
        # Do not resurrect it merely because old/recomputed evidence reappears.
        if rows:
            return None
        return await self.create(
            instance_id=instance_id,
            text=clean,
            priority=self._evidence_priority(salience),
            source_node_id=source_node_id,
            meta=evidence_meta,
        )

    async def _reconcile_terminal_threads(
        self, instance_id: UUID, goal_id: UUID | None, status: str,
        *, resolution_kind: str | None = None,
    ) -> None:
        if goal_id is None or status not in {"completed", "cancelled"}:
            return
        reason = resolution_kind or ("cancelled" if status == "cancelled" else "goal_completed")
        await self.db.execute(
            """UPDATE aios.character_cognitive_thread
               SET status='resolved',resolved_at=COALESCE(resolved_at,now()),
                   pressure=0,meta=meta || $3::jsonb,updated_at=now()
               WHERE instance_id=$1 AND goal_id=$2 AND status<>'resolved'""",
            instance_id, goal_id,
            json.dumps({"resolution_kind": reason, "resolved_by": "goal_lifecycle"}),
        )

    async def _refresh_scene(self, instance_id: UUID) -> None:
        # Goal lifecycle owns intention; scene projection consumes it.
        from aios_app.epistemic.scene_resolver import CharacterSceneProjector
        await CharacterSceneProjector(self.db).refresh(instance_id)

    async def _invalidate(self, instance_id: UUID) -> None:
        """Make executive-state mutations visible even without DAG movement."""
        await self.db.execute(
            """UPDATE aios.character_runtime_state
               SET state_version=state_version+1,updated_at=now()
               WHERE instance_id=$1""",
            instance_id,
        )
        await self.db.execute(
            """UPDATE aios.character_hud_readiness
               SET status='dirty',dirty_since=COALESCE(dirty_since,now()),updated_at=now()
               WHERE instance_id=$1""",
            instance_id,
        )

    @staticmethod
    def _evidence_priority(salience: float | None) -> int:
        if salience is None:
            return 100
        bounded = max(0.0, min(1.0, float(salience)))
        return max(10, min(100, int(round(100 - bounded * 70))))

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
