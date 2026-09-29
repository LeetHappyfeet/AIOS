from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aios_app.db import Database
from .runtime import AgentRuntimeStore


@dataclass(frozen=True)
class GoalTimeWindow:
    due_at: datetime
    window_end_at: datetime
    timezone_name: str | None
    expression: str


_RELATIVE_DURATION = re.compile(
    r"^in\s+(\d{1,4})\s+(minute|minutes|hour|hours|day|days|week|weeks)$", re.I
)
_DAY_WINDOW = re.compile(r"^(today|tomorrow)(?:\s+(morning|afternoon|evening|tonight))?$", re.I)
_VAGUE_FUTURE = {"later", "soon", "afterwards", "sometime later"}
_ALLOWED_DEFER_SECONDS = {900, 3600, 14400, 86400}


def resolve_goal_time_expression(
    *, expression: str, source_text: str, source_at: datetime,
    timezone_name: str | None, suggested_delay_seconds: int | None = None,
) -> GoalTimeWindow | None:
    """Resolve only explicit, bounded expressions grounded in the utterance."""
    phrase = " ".join(str(expression or "").split()).strip().lower()
    source = " ".join(str(source_text or "").split()).lower()
    if not phrase or phrase not in source or source_at.tzinfo is None:
        return None
    duration = _RELATIVE_DURATION.fullmatch(phrase)
    if duration:
        count = int(duration.group(1))
        unit = duration.group(2).lower().rstrip("s")
        delta = {
            "minute": timedelta(minutes=count), "hour": timedelta(hours=count),
            "day": timedelta(days=count), "week": timedelta(weeks=count),
        }[unit]
        due = source_at.astimezone(timezone.utc) + delta
        end = due
        resolved_timezone = None
    elif phrase in _VAGUE_FUTURE and suggested_delay_seconds in _ALLOWED_DEFER_SECONDS:
        due = source_at.astimezone(timezone.utc) + timedelta(seconds=suggested_delay_seconds)
        end = due
        resolved_timezone = None
    else:
        match = _DAY_WINDOW.fullmatch(phrase)
        if not match or not timezone_name:
            return None
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return None
        day, period = match.groups()
        local_source = source_at.astimezone(zone)
        target_date = local_source.date() + (timedelta(days=1) if day == "tomorrow" else timedelta())
        windows = {
            None: (time(9), time(17)),
            "morning": (time(8), time(12)),
            "afternoon": (time(12), time(17)),
            "evening": (time(17), time(21)),
            "tonight": (time(18), time(23, 59)),
        }
        start_time, end_time = windows[period]
        due_local = datetime.combine(target_date, start_time, tzinfo=zone)
        end_local = datetime.combine(target_date, end_time, tzinfo=zone)
        due, end = due_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
        resolved_timezone = timezone_name
    now = datetime.now(timezone.utc)
    if due <= now or due > now + timedelta(days=366) or end < due:
        return None
    return GoalTimeWindow(due, end, resolved_timezone, phrase)


class TemporalTriggerStore:
    """Durable wall-clock intent which delivers into the existing wake inbox."""

    def __init__(self, db: Database):
        self.db = db
        self.runtime = AgentRuntimeStore(db)

    async def schedule_goal_review(
        self, *, instance_id: UUID, goal_id: UUID, due_at: datetime,
        window_end_at: datetime | None, timezone_name: str | None,
        reason: str, payload: Mapping[str, Any], dedupe_key: str,
    ) -> dict[str, Any] | None:
        """Atomically park an active goal behind its one-shot review trigger."""
        due_at = self._aware_utc(due_at)
        end_at = self._aware_utc(window_end_at) if window_end_at else None
        assert due_at is not None
        if end_at is not None and end_at < due_at:
            raise ValueError("goal review window ends before it starts")
        if due_at <= datetime.now(timezone.utc):
            raise ValueError("goal review time must be in the future")
        if timezone_name:
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone_name must be a valid IANA timezone") from exc

        async with self.db.connection() as con:
            async with con.transaction():
                goal = await con.fetchrow(
                    """SELECT goal_id,status FROM aios.character_agent_goal
                       WHERE instance_id=$1 AND goal_id=$2 FOR UPDATE""",
                    instance_id, goal_id,
                )
                if not goal or str(goal["status"]) != "active":
                    return None
                row = await con.fetchrow(
                    """INSERT INTO aios.character_temporal_trigger(
                           instance_id,goal_id,trigger_type,clock_type,status,due_at,
                           window_end_at,timezone,event_type,reason,payload,priority,dedupe_key)
                       VALUES($1,$2,'timer','wall','scheduled',$3,$4,$5,
                              'GOAL_REVIEW_DUE',$6,$7::jsonb,20,$8)
                       ON CONFLICT(instance_id,dedupe_key) DO NOTHING
                       RETURNING *""",
                    instance_id, goal_id, due_at, end_at, timezone_name,
                    str(reason or "Future goal review"), json.dumps(dict(payload), default=str),
                    dedupe_key,
                )
                if row is None:
                    row = await con.fetchrow(
                        """SELECT * FROM aios.character_temporal_trigger
                           WHERE instance_id=$1 AND dedupe_key=$2""",
                        instance_id, dedupe_key,
                    )
                    return self._public(dict(row)) if row else None
                await con.execute(
                    """UPDATE aios.character_agent_goal
                       SET status='scheduled',updated_at=now(),
                           meta=meta || $3::jsonb
                       WHERE instance_id=$1 AND goal_id=$2 AND status='active'""",
                    instance_id, goal_id,
                    json.dumps({"temporal_trigger_id": str(row["trigger_id"]),
                                "scheduled_due_at": due_at.isoformat(),
                                "scheduled_window_end_at": end_at.isoformat() if end_at else None,
                                "scheduled_timezone": timezone_name,
                                "scheduled_reason": str(reason or "Future goal review")}),
                )
        from aios_app.epistemic.goals import CharacterGoalService
        goals = CharacterGoalService(self.db)
        await goals._invalidate(instance_id)
        await goals._refresh_scene(instance_id)
        return self._public(dict(row))

    @staticmethod
    def _aware_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("temporal timestamps must include a timezone offset")
        return value.astimezone(timezone.utc)

    async def create_timer(
        self, *, instance_id: UUID, reason: str,
        duration_seconds: int | None = None, due_at: datetime | None = None,
        payload: Mapping[str, Any] | None = None, priority: int = 100,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        if (duration_seconds is None) == (due_at is None):
            raise ValueError("timer requires exactly one of duration_seconds or due_at")
        if duration_seconds is not None:
            seconds = int(duration_seconds)
            if seconds < 1:
                raise ValueError("duration_seconds must be at least 1")
            due_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        assert due_at is not None
        if due_at.tzinfo is None:
            raise ValueError("due_at must include a timezone offset")
        due_at = due_at.astimezone(timezone.utc)
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.character_temporal_trigger (
                   instance_id,trigger_type,clock_type,due_at,event_type,reason,
                   payload,priority,dedupe_key
               ) VALUES ($1,'timer','wall',$2,'TIMER_DUE',$3,$4::jsonb,$5,$6)
               ON CONFLICT (instance_id,dedupe_key) DO NOTHING
               RETURNING *""",
            instance_id, due_at, str(reason or "").strip(),
            json.dumps(dict(payload or {}), default=str), int(priority), dedupe_key,
        )
        if row is None and dedupe_key is not None:
            row = await self.db.fetchrow(
                """SELECT * FROM aios.character_temporal_trigger
                   WHERE instance_id=$1 AND dedupe_key=$2""",
                instance_id, dedupe_key,
            )
        if row is None:
            raise RuntimeError("timer could not be created")
        return self._public(dict(row))

    async def cancel(self, *, instance_id: UUID, trigger_id: UUID) -> dict[str, Any]:
        async with self.db.connection() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    """UPDATE aios.character_temporal_trigger
                       SET status='cancelled',cancelled_at=now(),
                           last_evaluated_at=now(),last_outcome='cancelled_by_request',updated_at=now()
                       WHERE trigger_id=$1 AND instance_id=$2 AND status='scheduled'
                       RETURNING *""",
                    trigger_id, instance_id,
                )
                if row is None:
                    row = await con.fetchrow(
                        """SELECT * FROM aios.character_temporal_trigger
                           WHERE trigger_id=$1 AND instance_id=$2""",
                        trigger_id, instance_id,
                    )
                    if row is None:
                        raise LookupError(f"unknown timer {trigger_id}")
                elif row["goal_id"]:
                    await con.execute(
                        """UPDATE aios.character_agent_goal
                           SET status='active',updated_at=now(),
                               meta=meta || jsonb_build_object('last_temporal_outcome','timer_cancelled')
                           WHERE instance_id=$1 AND goal_id=$2 AND status='scheduled'""",
                        instance_id, row["goal_id"],
                    )
        if row["goal_id"]:
            from aios_app.epistemic.goals import CharacterGoalService
            goals = CharacterGoalService(self.db)
            await goals._invalidate(instance_id)
            await goals._refresh_scene(instance_id)
        return self._public(dict(row))

    async def list(self, *, instance_id: UUID, include_terminal: bool = False,
                   limit: int = 32) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """SELECT * FROM aios.character_temporal_trigger
               WHERE instance_id=$1
                 AND ($2::boolean OR status='scheduled')
               ORDER BY CASE WHEN status='scheduled' THEN 0 ELSE 1 END,due_at DESC
               LIMIT $3""",
            instance_id, bool(include_terminal), max(1, min(int(limit), 100)),
        )
        return [self._public(dict(row)) for row in rows]

    async def emit_due(self, limit: int = 100) -> int:
        # Claim first. A crashed process leaves 'firing' rows recoverable by
        # reset_stale_firing(); normal retries cannot double-claim them.
        rows = await self.db.fetch(
            """WITH due AS (
                   SELECT trigger_id
                   FROM aios.character_temporal_trigger
                   WHERE status='scheduled' AND clock_type='wall' AND due_at <= now()
                   ORDER BY priority,due_at
                   FOR UPDATE SKIP LOCKED
                   LIMIT $1
               )
               UPDATE aios.character_temporal_trigger t
               SET status='firing',updated_at=now()
               FROM due
               WHERE t.trigger_id=due.trigger_id
               RETURNING t.*""",
            max(1, min(int(limit), 1000)),
        )
        emitted = 0
        for raw in rows:
            row = dict(raw)
            trigger_id = row["trigger_id"]
            payload = dict(row.get("payload") or {})
            payload.update({
                "trigger_id": str(trigger_id),
                "trigger_type": row["trigger_type"],
                "reason": row.get("reason"),
                "due_at": row["due_at"].isoformat(),
                "window_end_at": row["window_end_at"].isoformat()
                    if row.get("window_end_at") else None,
                "timezone": row.get("timezone"),
                "goal_id": str(row["goal_id"]) if row.get("goal_id") else None,
            })
            try:
                if row.get("goal_id"):
                    if await self._fire_goal_review(row, payload):
                        emitted += 1
                    continue
                wake_id = await self.runtime.wake(
                    instance_id=row["instance_id"],
                    event_type=str(row["event_type"]),
                    source_type="temporal_trigger",
                    source_id=str(trigger_id),
                    payload=payload,
                    priority=int(row["priority"]),
                    dedupe_key=f"temporal:{trigger_id}:0",
                )
                await self.db.execute(
                    """UPDATE aios.character_temporal_trigger
                       SET status='fired',fired_at=now(),updated_at=now(),
                           payload=payload || jsonb_build_object('wake_id',$2::text)
                       WHERE trigger_id=$1 AND status='firing'""",
                    trigger_id, wake_id,
                )
                emitted += 1
            except Exception:
                await self.db.execute(
                    """UPDATE aios.character_temporal_trigger
                       SET status='scheduled',updated_at=now()
                       WHERE trigger_id=$1 AND status='firing'""",
                    trigger_id,
                )
                raise
        return emitted

    async def _fire_goal_review(
        self, trigger: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> bool:
        """Deterministically reactivate a still-scheduled goal and wake it once."""
        trigger_id = trigger["trigger_id"]
        instance_id = trigger["instance_id"]
        goal_id = trigger["goal_id"]
        dedupe = f"temporal:{trigger_id}:0"
        async with self.db.connection() as con:
            async with con.transaction():
                goal = await con.fetchrow(
                    """UPDATE aios.character_agent_goal
                       SET status='active',updated_at=now(),
                           meta=meta || jsonb_build_object('last_temporal_outcome','review_due')
                       WHERE instance_id=$1 AND goal_id=$2 AND status='scheduled'
                       RETURNING goal_id,goal_text""",
                    instance_id, goal_id,
                )
                if not goal:
                    await con.execute(
                        """UPDATE aios.character_temporal_trigger
                           SET status='completed',last_evaluated_at=now(),
                               last_outcome='goal_no_longer_scheduled',updated_at=now()
                           WHERE trigger_id=$1 AND status='firing'""",
                        trigger_id,
                    )
                    return False
                wake_payload = dict(payload)
                wake_payload.update({"goal_id": str(goal_id), "goal_text": goal["goal_text"]})
                await con.execute(
                    """INSERT INTO aios.character_agent_runtime(instance_id)
                       VALUES($1) ON CONFLICT(instance_id) DO NOTHING""",
                    instance_id,
                )
                wake = await con.fetchrow(
                    """INSERT INTO aios.character_wake_event(
                           instance_id,event_type,source_type,source_id,payload,priority,dedupe_key)
                       VALUES($1,'GOAL_REVIEW_DUE','temporal_trigger',$2,$3::jsonb,$4,$5)
                       ON CONFLICT(instance_id,dedupe_key) DO NOTHING
                       RETURNING wake_id""",
                    instance_id, str(trigger_id), json.dumps(wake_payload, default=str),
                    int(trigger["priority"]), dedupe,
                )
                if wake is None:
                    wake = await con.fetchrow(
                        """SELECT wake_id FROM aios.character_wake_event
                           WHERE instance_id=$1 AND dedupe_key=$2""",
                        instance_id, dedupe,
                    )
                await con.execute(
                    """UPDATE aios.character_agent_runtime
                       SET state=CASE WHEN state='paused' THEN state ELSE 'ready' END,
                           last_wake_at=now(),updated_at=now()
                       WHERE instance_id=$1""",
                    instance_id,
                )
                await con.execute(
                    """UPDATE aios.character_temporal_trigger
                       SET status='fired',fired_at=now(),last_evaluated_at=now(),
                           last_outcome='goal_reactivated',updated_at=now(),
                           payload=payload || jsonb_build_object('wake_id',$2::text)
                       WHERE trigger_id=$1 AND status='firing'""",
                    trigger_id, str(wake["wake_id"]),
                )
        from aios_app.epistemic.goals import CharacterGoalService
        goals = CharacterGoalService(self.db)
        await goals._invalidate(instance_id)
        await goals._refresh_scene(instance_id)
        return True

    async def reset_stale_firing(self, stale_seconds: int = 300) -> int:
        result = await self.db.execute(
            """UPDATE aios.character_temporal_trigger
               SET status='scheduled',updated_at=now()
               WHERE status='firing'
                 AND updated_at < now() - ($1 * interval '1 second')""",
            max(30, int(stale_seconds)),
        )
        try:
            return int(str(result).rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "trigger_id": str(row["trigger_id"]),
            "trigger_type": row["trigger_type"],
            "clock_type": row["clock_type"],
            "status": row["status"],
            "due_at": row["due_at"].isoformat() if row.get("due_at") else None,
            "reason": row.get("reason"),
            "event_type": row["event_type"],
            "priority": row["priority"],
            "goal_id": str(row["goal_id"]) if row.get("goal_id") else None,
            "window_end_at": row["window_end_at"].isoformat() if row.get("window_end_at") else None,
            "timezone": row.get("timezone"),
            "last_outcome": row.get("last_outcome"),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        }
