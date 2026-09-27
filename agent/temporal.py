from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from .runtime import AgentRuntimeStore


class TemporalTriggerStore:
    """Durable wall-clock intent which delivers into the existing wake inbox."""

    def __init__(self, db: Database):
        self.db = db
        self.runtime = AgentRuntimeStore(db)

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
        row = await self.db.execute_returning_row(
            """UPDATE aios.character_temporal_trigger
               SET status='cancelled',cancelled_at=now(),updated_at=now()
               WHERE trigger_id=$1 AND instance_id=$2 AND status='scheduled'
               RETURNING *""",
            trigger_id, instance_id,
        )
        if row is None:
            existing = await self.db.fetchrow(
                """SELECT * FROM aios.character_temporal_trigger
                   WHERE trigger_id=$1 AND instance_id=$2""",
                trigger_id, instance_id,
            )
            if existing is None:
                raise LookupError(f"unknown timer {trigger_id}")
            return self._public(dict(existing))
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
            })
            try:
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
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        }
