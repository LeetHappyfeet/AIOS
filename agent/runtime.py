from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database


class AgentRuntimeStore:
    def __init__(self, db: Database):
        self.db = db

    async def ensure(self, instance_id: UUID) -> None:
        await self.db.execute(
            """
            INSERT INTO aios.character_agent_runtime (instance_id)
            VALUES ($1) ON CONFLICT (instance_id) DO NOTHING
            """,
            instance_id,
        )

    async def wake(
        self, *, instance_id: UUID, event_type: str,
        source_type: str | None = None, source_id: str | None = None,
        payload: Mapping[str, Any] | None = None, priority: int = 100,
        dedupe_key: str | None = None,
    ) -> UUID:
        await self.ensure(instance_id)
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.character_wake_event (
                instance_id, event_type, source_type, source_id, payload,
                priority, dedupe_key
            ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)
            ON CONFLICT (instance_id, dedupe_key) DO NOTHING
            RETURNING wake_id
            """,
            instance_id, event_type, source_type, source_id,
            json.dumps(dict(payload or {}), default=str), int(priority), dedupe_key,
        )
        if row:
            wake_id = row["wake_id"]
        else:
            existing = await self.db.fetchrow(
                "SELECT wake_id FROM aios.character_wake_event WHERE instance_id=$1 AND dedupe_key=$2",
                instance_id, dedupe_key,
            )
            if not existing:
                raise RuntimeError("wake event conflict could not be resolved")
            wake_id = existing["wake_id"]
        await self.db.execute(
            """
            UPDATE aios.character_agent_runtime
            SET state=CASE WHEN state='paused' THEN state ELSE 'ready' END,
                last_wake_at=now(), updated_at=now()
            WHERE instance_id=$1
            """,
            instance_id,
        )
        return wake_id

    async def claim_next(self, instance_id: UUID) -> dict[str, Any] | None:
        await self.ensure(instance_id)
        row = await self.db.execute_returning_row(
            """
            UPDATE aios.character_wake_event w
            SET status='claimed', claimed_at=now()
            WHERE w.wake_id = (
                SELECT wake_id FROM aios.character_wake_event
                WHERE instance_id=$1 AND status='pending' AND available_at <= now()
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING *
            """,
            instance_id,
        )
        return dict(row) if row else None

    async def consume(self, wake_id: UUID) -> None:
        await self.db.execute(
            """
            UPDATE aios.character_wake_event
            SET status='consumed', consumed_at=now()
            WHERE wake_id=$1 AND status='claimed'
            """,
            wake_id,
        )

    async def configure_heartbeat(
        self, instance_id: UUID, *, enabled: bool, interval_seconds: int = 300
    ) -> None:
        interval_seconds = max(60, int(interval_seconds))
        await self.ensure(instance_id)
        await self.db.execute(
            """
            UPDATE aios.character_agent_runtime
            SET heartbeat_enabled=$2, heartbeat_interval_seconds=$3,
                next_heartbeat_at=CASE
                    WHEN $2 THEN now() + ($3 * interval '1 second') ELSE NULL
                END,
                updated_at=now()
            WHERE instance_id=$1
            """,
            instance_id, enabled, interval_seconds,
        )

    async def emit_due_heartbeats(self, limit: int = 100) -> int:
        rows = await self.db.fetch(
            """
            SELECT instance_id, heartbeat_interval_seconds, next_heartbeat_at
            FROM aios.character_agent_runtime
            WHERE heartbeat_enabled=true AND state <> 'paused'
              AND next_heartbeat_at IS NOT NULL AND next_heartbeat_at <= now()
            ORDER BY next_heartbeat_at LIMIT $1
            """,
            max(1, min(int(limit), 1000)),
        )
        emitted = 0
        for row in rows:
            due = row["next_heartbeat_at"]
            key = f"heartbeat:{due.isoformat()}"
            await self.wake(
                instance_id=row["instance_id"], event_type="HEARTBEAT",
                source_type="heartbeat", source_id=key,
                payload={"scheduled_at": due.isoformat()}, priority=500,
                dedupe_key=key,
            )
            await self.db.execute(
                """
                UPDATE aios.character_agent_runtime
                SET next_heartbeat_at=now() + (heartbeat_interval_seconds * interval '1 second'),
                    updated_at=now()
                WHERE instance_id=$1
                """,
                row["instance_id"],
            )
            emitted += 1
        return emitted
