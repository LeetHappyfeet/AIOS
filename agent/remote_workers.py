from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from .lifecycle import ActionRecord, CharacterAgencyStore
from .runtime import AgentRuntimeStore


class RemoteWorkerStore:
    """Control-plane registry and lease broker for externally hosted workers."""

    def __init__(self, db: Database):
        self.db = db
        self.agency = CharacterAgencyStore(db)
        self.runtime = AgentRuntimeStore(db)

    async def register(self, *, worker_key: str, worker_type: str,
                       capabilities: list[str], max_concurrency: int = 1,
                       labels: Mapping[str, Any] | None = None) -> dict[str, Any]:
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.remote_worker
               (worker_key,worker_type,capabilities,max_concurrency,labels,status,last_heartbeat_at)
               VALUES ($1,$2,$3::text[],$4,$5::jsonb,'ready',now())
               ON CONFLICT (worker_key) DO UPDATE SET
                 worker_type=EXCLUDED.worker_type,
                 capabilities=EXCLUDED.capabilities,
                 max_concurrency=EXCLUDED.max_concurrency,
                 labels=EXCLUDED.labels,
                 status=CASE WHEN aios.remote_worker.status='draining' THEN 'draining' ELSE 'ready' END,
                 last_heartbeat_at=now(), updated_at=now()
               RETURNING *""",
            worker_key, worker_type, capabilities, max(1, int(max_concurrency)),
            json.dumps(dict(labels or {}), default=str),
        )
        return dict(row)

    async def heartbeat(self, worker_id: UUID) -> dict[str, Any]:
        row = await self.db.execute_returning_row(
            """UPDATE aios.remote_worker SET last_heartbeat_at=now(),
                   status=CASE WHEN status='offline' THEN 'ready' ELSE status END,
                   updated_at=now()
               WHERE worker_id=$1 RETURNING *""", worker_id,
        )
        if not row:
            raise LookupError("unknown remote worker")
        return dict(row)

    async def claim(self, worker_id: UUID, *, lease_seconds: int = 120) -> ActionRecord | None:
        lease_seconds = max(30, min(int(lease_seconds), 3600))
        # Reclaim abandoned work before selecting a new action. A stale worker
        # cannot report completion after reassignment because ownership changes.
        await self.db.execute(
            """UPDATE aios.character_action
               SET status='queued', assigned_worker_id=NULL, lease_expires_at=NULL,
                   updated_at=now()
               WHERE execution_mode='worker' AND status='running'
                 AND lease_expires_at IS NOT NULL AND lease_expires_at <= now()"""
        )
        row = await self.db.execute_returning_row(
            """WITH worker AS (
                   SELECT worker_id,capabilities,max_concurrency
                   FROM aios.remote_worker
                   WHERE worker_id=$1 AND status='ready'
                     AND last_heartbeat_at > now() - interval '5 minutes'
               ), capacity AS (
                   SELECT w.* FROM worker w
                   WHERE (SELECT count(*) FROM aios.character_action a
                          WHERE a.assigned_worker_id=w.worker_id
                            AND a.status='running'
                            AND a.lease_expires_at > now()) < w.max_concurrency
               ), candidate AS (
                   SELECT a.action_id
                   FROM aios.character_action a, capacity w
                   WHERE a.status='queued' AND a.execution_mode='worker'
                     AND a.action_type = ANY(w.capabilities)
                   ORDER BY a.created_at, a.action_id
                   FOR UPDATE OF a SKIP LOCKED
                   LIMIT 1
               )
               UPDATE aios.character_action a
               SET status='running', assigned_worker_id=$1,
                   lease_expires_at=now() + ($2 * interval '1 second'),
                   started_at=COALESCE(started_at,now()), updated_at=now()
               FROM candidate c WHERE a.action_id=c.action_id
               RETURNING a.*""",
            worker_id, lease_seconds,
        )
        return ActionRecord.from_row(row) if row else None

    async def complete(self, worker_id: UUID, action_id: UUID,
                       result: Mapping[str, Any]) -> ActionRecord:
        action = await self.agency.get_action(action_id)
        if not action or action.assigned_worker_id != worker_id or action.status != "running":
            raise PermissionError("worker does not own this running action")
        action = await self.agency.transition_action(action_id, "succeeded", result=result)
        await self.runtime.wake(
            instance_id=action.instance_id, event_type="ACTION_COMPLETED",
            source_type="remote_worker", source_id=str(action_id),
            payload={"action_id": str(action_id), "task_id": str(action.task_id or ""),
                     "status": "succeeded", "result": dict(result)},
            dedupe_key=f"remote-action:{action_id}:succeeded",
        )
        return action

    async def fail(self, worker_id: UUID, action_id: UUID, error: str) -> ActionRecord:
        action = await self.agency.get_action(action_id)
        if not action or action.assigned_worker_id != worker_id or action.status != "running":
            raise PermissionError("worker does not own this running action")
        action = await self.agency.transition_action(action_id, "failed", error=error[:2000])
        await self.runtime.wake(
            instance_id=action.instance_id, event_type="ACTION_FAILED",
            source_type="remote_worker", source_id=str(action_id),
            payload={"action_id": str(action_id), "task_id": str(action.task_id or ""),
                     "status": "failed", "error": error[:2000]},
            dedupe_key=f"remote-action:{action_id}:failed",
        )
        return action
