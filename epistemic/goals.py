from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.goal_source_admission import review_goal_source, ADMISSION_VERSION


def valid_goal_objective(value: str | None) -> bool:
    """Reject incomplete infinitives left by prose/markup sentence splitting."""
    objective = " ".join(str(value or "").split())
    if not objective or not re.search(r"[A-Za-z]", objective):
        return False
    if re.match(r"(?i)^to\b", objective) and not re.match(
        r"(?i)^to\s+[A-Za-z]", objective
    ):
        return False
    return True


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
        await self._carry_persistent(instance_id)
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

    async def list_scheduled(self, instance_id: UUID, *, limit: int = 8) -> list[dict[str, Any]]:
        """Return future goals without promoting them into active attention."""
        rows = await self.db.fetch(
            """SELECT g.goal_id,g.goal_text,g.priority,g.meta,t.due_at,
                      t.window_end_at,t.timezone,t.reason,t.time_expression
               FROM aios.character_agent_goal g
               LEFT JOIN LATERAL (
                 SELECT due_at,window_end_at,timezone,reason,payload->>'time_expression' AS time_expression
                 FROM aios.character_temporal_trigger
                 WHERE goal_id=g.goal_id AND status='scheduled'
                 ORDER BY due_at LIMIT 1
               ) t ON true
               WHERE g.instance_id=$1 AND g.status='scheduled'
               ORDER BY t.due_at NULLS LAST,g.priority,g.created_at
               LIMIT $2""",
            instance_id, max(1, min(int(limit), 32)),
        )
        return [
            {
                "goal_id": str(row["goal_id"]),
                "text": str(row["goal_text"]),
                "priority": int(row["priority"]),
                "status": "scheduled",
                "due_at": row["due_at"].isoformat() if row["due_at"] else None,
                "window_end_at": row["window_end_at"].isoformat() if row["window_end_at"] else None,
                "timezone": row["timezone"],
                "reason": row["reason"],
                "time_expression": row["time_expression"],
            }
            for row in rows
        ]

    async def _carry_persistent(self, instance_id: UUID) -> None:
        """Bring forward explicit standing intentions within the same character/user.

        Session and scene intentions never cross this boundary. The unique
        provenance key also prevents concurrent HUD requests from duplicating
        an inherited goal or reviving one that was completed locally.
        """
        await self.db.execute(
            """WITH target AS (
                 SELECT character_id,world_id,
                        meta->>'runtime_user_name' AS user_name,created_at
                 FROM aios.character_instance WHERE instance_id=$1
               ), candidates AS (
                 SELECT DISTINCT ON (COALESCE(g.meta->>'inherited_from_goal_id',g.goal_id::text))
                        g.goal_id,g.instance_id,g.goal_text,g.priority,g.meta,g.status,
                        COALESCE(g.meta->>'inherited_from_goal_id',g.goal_id::text) AS root_id
                 FROM aios.character_agent_goal g
                 JOIN aios.character_instance source ON source.instance_id=g.instance_id
                 CROSS JOIN target t
                 WHERE g.meta->>'horizon'='persistent'
                   AND source.character_id=t.character_id
                   AND (source.world_id IS NULL OR t.world_id IS NULL
                        OR source.world_id=t.world_id)
                   AND source.created_at<t.created_at
                   AND g.created_at<t.created_at AND g.updated_at<t.created_at
                   AND t.user_name IS NOT NULL AND t.user_name<>''
                   AND source.meta->>'runtime_user_name'=t.user_name
                   AND COALESCE(g.meta->>'created_by','') NOT IN
                       ('legacy_runtime_goal_import','goal_backfill')
                 ORDER BY COALESCE(g.meta->>'inherited_from_goal_id',g.goal_id::text),
                          source.created_at DESC,g.created_at DESC
               )
               INSERT INTO aios.character_agent_goal(instance_id,goal_text,priority,meta)
               SELECT $1,c.goal_text,c.priority,
                      (c.meta-'expectation'-'outcome_decision'-'resolution_kind'-'retry_request_id') || jsonb_build_object('inherited_from_goal_id',c.root_id,
                                                  'inherited_from_instance_id',c.instance_id::text)
               FROM candidates c WHERE c.status='active'
               ORDER BY c.priority,c.goal_id LIMIT 5
               ON CONFLICT (instance_id,(meta->>'inherited_from_goal_id'))
                 WHERE meta ? 'inherited_from_goal_id' DO NOTHING""",
            instance_id,
        )

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
        refresh_scene: bool = True,
    ) -> CognitiveGoal:
        clean = " ".join(str(text).split())
        if not clean:
            raise ValueError("goal text cannot be empty")
        goal_meta = dict(meta or {})
        if "expectation" in goal_meta:
            from aios_app.agent.reinforcement import validate_expectation
            if not isinstance(goal_meta["expectation"], Mapping):
                raise ValueError("expectation must be an object")
            goal_meta["expectation"] = validate_expectation(goal_meta["expectation"])
        if "origin_scene" not in goal_meta:
            origin_scene = await self._origin_scene(instance_id, source_node_id)
            if origin_scene:
                goal_meta["origin_scene"] = origin_scene
        row = await self._returning(
            """INSERT INTO aios.character_agent_goal(
                   instance_id,source_task_id,source_node_id,root_task_id,source_action_id,
                   goal_text,priority,meta)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
               RETURNING goal_id,goal_text,status,priority,meta""",
            instance_id, source_task_id, source_node_id, root_task_id, source_action_id,
            clean, int(priority), json.dumps(goal_meta),
        )
        goal = self._goal(row)
        await self._invalidate(instance_id)
        if refresh_scene:
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
        self, *, instance_id: UUID, goal_id: UUID, status: str = "completed",
        refresh_scene: bool = True, resolution_kind: str | None = None,
        verification: str = "unverified", source_node_id: UUID | None = None,
        evidence_ids: list[str] | None = None, attempt_id: UUID | None = None,
    ) -> CognitiveGoal:
        """One status mutation; the database atomically owns its durable effects.

        Verified appraisal is admitted separately by OutcomeResolver. Neither
        callers nor model-visible goal actions can upgrade evidence authority.
        """
        if status not in {"completed", "failed", "cancelled", "dormant"}:
            raise ValueError("invalid goal status")
        if verification not in {"unverified", "reviewed", "projected"}:
            raise ValueError("goal finish cannot assert verified authority")
        decision = {"resolution_kind": resolution_kind or status,
                    "outcome_decision": {"verification": verification,
                        "source_node_id": str(source_node_id) if source_node_id else None,
                        "evidence_ids": list(evidence_ids or [])}}
        row = await self._returning(
            """UPDATE aios.character_agent_goal
               SET status=$3,meta=meta || $4::jsonb,
                   completed_at=CASE WHEN $3 IN ('completed','failed','cancelled') THEN now() ELSE NULL END,
                   updated_at=now()
               WHERE goal_id=$1 AND instance_id=$2 AND status IN ('active','scheduled','dormant')
                 AND ($5::uuid IS NULL OR EXISTS (SELECT 1 FROM aios.character_goal_attempt a
                      WHERE a.goal_id=$1 AND a.attempt_id=$5 AND a.closed_at IS NULL))
               RETURNING goal_id,goal_text,status,priority,meta""",
            goal_id, instance_id, status, json.dumps(decision), attempt_id,
        )
        if not row:
            raise LookupError("open goal not found")
        goal = self._goal(row)
        if refresh_scene:
            await self._refresh_scene(instance_id)
        return goal

    async def retry(
        self, *, instance_id: UUID, goal_id: UUID, request_id: UUID,
        refresh_scene: bool = True,
    ) -> CognitiveGoal:
        """Explicit idempotent retry; timer reactivation never starts an attempt."""
        row = await self._returning(
            """UPDATE aios.character_agent_goal
               SET status='active',completed_at=NULL,updated_at=now(),
                   meta=(meta-'outcome_decision'-'resolution_kind'-'expectation') ||
                        jsonb_build_object('retry_request_id',$3::text)
               WHERE goal_id=$1 AND instance_id=$2 AND status IN ('completed','failed','cancelled')
                 AND NOT EXISTS (SELECT 1 FROM aios.character_goal_attempt
                                 WHERE goal_id=$1 AND retry_request_id=$3)
               RETURNING goal_id,goal_text,status,priority,meta""",
            goal_id, instance_id, request_id,
        )
        if not row:
            row = await self.db.fetchrow(
                """SELECT g.goal_id,g.goal_text,g.status,g.priority,g.meta
                   FROM aios.character_agent_goal g
                   JOIN aios.character_goal_attempt a ON a.goal_id=g.goal_id
                   WHERE g.goal_id=$1 AND g.instance_id=$2 AND a.retry_request_id=$3""",
                goal_id, instance_id, request_id,
            )
        if not row:
            raise LookupError("terminal goal not found or retry is not eligible")
        if refresh_scene:
            await self._refresh_scene(instance_id)
        return self._goal(row)

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
        intent_type: str | None = None,
        horizon: str | None = None,
        objective: str | None = None,
        source_text: str | None = None,
        parse_reason: str | None = None,
        source_span: list[int] | tuple[int, int] | None = None,
        refresh_scene: bool = True,
    ) -> CognitiveGoal | None:
        """Project character-owned GOAL evidence into managed intention state.

        topic_key is the stable bridge between epistemic evidence and executive
        state. Positive evidence creates/refreshes one active managed goal.
        Negative evidence cancels the active goal for that topic. Historical
        rows are retained and are never silently reactivated.
        """
        topic = str(topic_key or "").strip()
        clean = " ".join(str(text or "").split())
        if not topic or not clean or not valid_goal_objective(objective or clean):
            return None
        # Shared lifecycle guard for live, deferred and enriched source units.
        # External executive actions without a source excerpt keep their own
        # preexisting authority; never assign authorship from canonical text.
        if source_text is not None:
            span = (tuple(source_span) if source_span is not None
                    and len(source_span) == 2 else None)
            admission = review_goal_source(
                source_text=source_text, objective=objective or clean,
                parse_reason=parse_reason, horizon=horizon,
                match_span=span,
            )
            if not admission.managed:
                return None
            # This is a source-eligibility receipt, NOT a semantic integrity or
            # goal-completion assertion. Replays reuse the original fingerprint.
            fingerprint = hashlib.sha256(json.dumps([
                str(instance_id), str(source_node_id), str(source_unit_id),
                source_text, objective or clean, polarity, ADMISSION_VERSION,
            ], ensure_ascii=False).encode("utf-8")).hexdigest()
            await self.db.execute(
                """INSERT INTO aios.character_goal_admission_receipt
                    (instance_id,source_node_id,source_unit_id,fingerprint,
                     objective,source_excerpt,decision,reason,policy_version)
                   VALUES ($1,$2,$3,$4,$5,$6,'eligible',$7,$8)
                   ON CONFLICT (instance_id,fingerprint) DO NOTHING""",
                instance_id, source_node_id, source_unit_id, fingerprint,
                objective or clean, str(source_text)[:1800],
                admission.reason, ADMISSION_VERSION,
            )
        rows = await self.db.fetch(
            """SELECT goal_id,goal_text,status,priority,meta
               FROM aios.character_agent_goal
               WHERE instance_id=$1
                 AND meta->>'semantic_topic_key'=$2
               ORDER BY created_at DESC, goal_id DESC""",
            instance_id, topic,
        )
        active = next((row for row in rows if str(row["status"]) in {"active", "scheduled"}), None)
        dormant = next((row for row in rows if str(row["status"]) == "dormant"), None)
        evidence_meta = {
            "semantic_topic_key": topic,
            "source": "message_cognition",
            "source_unit_id": str(source_unit_id) if source_unit_id else None,
            "confidence": confidence,
            "salience": salience,
            "intent_type": intent_type,
            "horizon": horizon,
            "objective": objective,
        }
        if polarity < 0:
            if active:
                try:
                    return await self.finish(
                        instance_id=instance_id, goal_id=active["goal_id"],
                        status="cancelled", resolution_kind="negated",
                        source_node_id=source_node_id,
                        evidence_ids=[str(source_unit_id)] if source_unit_id else [],
                        refresh_scene=refresh_scene,
                    )
                except LookupError:
                    return None
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
            if refresh_scene:
                await self._refresh_scene(instance_id)
            return goal

        if dormant:
            row = await self._returning(
                """UPDATE aios.character_agent_goal
                   SET status='active',goal_text=$3,
                       source_node_id=COALESCE($4,source_node_id),
                       completed_at=NULL,updated_at=now(),
                       meta=meta || $5::jsonb
                   WHERE goal_id=$1 AND instance_id=$2 AND status='dormant'
                   RETURNING goal_id,goal_text,status,priority,meta""",
                dormant["goal_id"], instance_id, clean, source_node_id,
                json.dumps({**evidence_meta, "reactivated_from": "dormant"}),
            )
            if row:
                goal = self._goal(row)
                await self._invalidate(instance_id)
                if refresh_scene:
                    await self._refresh_scene(instance_id)
                return goal

        # Completed/cancelled rows are terminal. Recomputed old evidence must
        # never resurrect them.
        if rows:
            return None
        return await self.create(
            instance_id=instance_id,
            text=clean,
            priority=self._evidence_priority(salience),
            source_node_id=source_node_id,
            meta=evidence_meta,
            refresh_scene=refresh_scene,
        )

    async def _reconcile_terminal_threads(
        self, instance_id: UUID, goal_id: UUID | None, status: str,
        *, resolution_kind: str | None = None,
    ) -> None:
        if goal_id is None or status not in {"completed", "failed", "cancelled"}:
            return
        reason = resolution_kind or {"cancelled":"cancelled", "failed":"goal_failed", "completed":"goal_completed"}[status]
        await self.db.execute(
            """UPDATE aios.character_cognitive_thread
               SET status='resolved',resolved_at=COALESCE(resolved_at,now()),
                   pressure=0,meta=meta || $3::jsonb,updated_at=now()
               WHERE instance_id=$1 AND goal_id=$2 AND status<>'resolved'""",
            instance_id, goal_id,
            json.dumps({"resolution_kind": reason, "resolved_by": "goal_lifecycle"}),
        )

    async def _origin_scene(
        self, instance_id: UUID, source_node_id: UUID | None
    ) -> dict[str, Any]:
        """Capture bounded branch-local scene provenance for lifecycle review."""
        if source_node_id is None:
            return {}
        row = await self.db.fetchrow(
            """SELECT snapshot_id,source_timeline_id,source_head_node_id,scene_state
               FROM aios.character_scene_snapshot
               WHERE instance_id=$1 AND source_head_node_id=$2
               ORDER BY updated_at DESC LIMIT 1""",
            instance_id, source_node_id)
        if not row:
            return {"source_node_id": str(source_node_id)}
        scene=self._json_object(row["scene_state"])
        return {
            "snapshot_id": str(row["snapshot_id"]),
            "source_node_id": str(row["source_head_node_id"] or source_node_id),
            "source_timeline_id": str(row["source_timeline_id"]) if row["source_timeline_id"] else None,
            "location": scene.get("location"),
            "present_entities": scene.get("present_entities") or [],
            "immediate_goal": scene.get("immediate_goal"),
            "pending_work": scene.get("pending_work"),
            "last_significant_change": scene.get("last_significant_change"),
        }

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
