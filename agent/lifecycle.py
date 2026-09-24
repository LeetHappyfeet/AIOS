from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from uuid import UUID

from aios_app.db import Database


TASK_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
ACTION_TERMINAL = frozenset({"succeeded", "failed", "rejected", "cancelled", "timed_out"})

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"waiting", "succeeded", "failed", "cancelled"}),
    "waiting": frozenset({"queued", "running", "succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

ACTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"validated", "rejected", "cancelled"}),
    "validated": frozenset({"queued", "running", "rejected", "cancelled"}),
    "queued": frozenset({"running", "cancelled", "timed_out"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "timed_out"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "rejected": frozenset(),
    "cancelled": frozenset(),
    "timed_out": frozenset(),
}


class InvalidLifecycleTransition(RuntimeError):
    pass


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), default=str)


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


@dataclass(frozen=True)
class CognitiveTask:
    task_id: UUID
    instance_id: UUID
    parent_task_id: Optional[UUID]
    task_type: str
    objective: str
    hud_profile_name: Optional[str]
    retrieval_focus: Optional[str]
    status: str
    priority: int
    trigger_type: Optional[str]
    trigger_id: Optional[str]
    source_state_version: Optional[int]
    source_node_id: Optional[UUID]
    source_from_node_id: Optional[UUID]
    source_through_node_id: Optional[UUID]
    source_scene_snapshot_id: Optional[UUID]
    result: Any
    error: Optional[str]
    meta: dict[str, Any]
    execution_mode: str = "auto"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CognitiveTask":
        return cls(
            task_id=row["task_id"],
            instance_id=row["instance_id"],
            parent_task_id=row["parent_task_id"],
            task_type=row["task_type"],
            objective=row["objective"],
            hud_profile_name=row["hud_profile_name"],
            retrieval_focus=row["retrieval_focus"],
            status=row["status"],
            priority=row["priority"],
            trigger_type=row["trigger_type"],
            trigger_id=row["trigger_id"],
            source_state_version=row["source_state_version"],
            source_node_id=row["source_node_id"],
            source_from_node_id=row.get("source_from_node_id"),
            source_through_node_id=row.get("source_through_node_id"),
            source_scene_snapshot_id=row.get("source_scene_snapshot_id"),
            result=_decode(row["result"]),
            error=row["error"],
            meta=dict(_decode(row["meta"]) or {}),
            execution_mode=str(row.get("execution_mode", "auto")),
        )


@dataclass(frozen=True)
class ActionRecord:
    action_id: UUID
    instance_id: UUID
    task_id: Optional[UUID]
    parent_action_id: Optional[UUID]
    action_type: str
    arguments: dict[str, Any]
    status: str
    side_effect_class: str
    idempotency_key: Optional[str]
    expected_state_version: Optional[int]
    proposed_by: Optional[str]
    result: Any
    error: Optional[str]
    rejection_reason: Optional[str]
    result_mode: str
    meta: dict[str, Any]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ActionRecord":
        return cls(
            action_id=row["action_id"],
            instance_id=row["instance_id"],
            task_id=row["task_id"],
            parent_action_id=row["parent_action_id"],
            action_type=row["action_type"],
            arguments=dict(_decode(row["arguments"]) or {}),
            status=row["status"],
            side_effect_class=row["side_effect_class"],
            idempotency_key=row["idempotency_key"],
            expected_state_version=row["expected_state_version"],
            proposed_by=row["proposed_by"],
            result=_decode(row["result"]),
            error=row["error"],
            rejection_reason=row["rejection_reason"],
            result_mode=str(row.get("result_mode", "terminal")),
            meta=dict(_decode(row["meta"]) or {}),
        )


class CharacterAgencyStore:
    """Durable lifecycle store for character cognitive work and actions.

    Transitions are compare-and-set updates. A stale or duplicate worker can
    therefore never move an object out of an unexpected state or resurrect a
    terminal task/action.
    """

    def __init__(self, db: Database):
        self.db = db

    async def create_task(
        self,
        *,
        instance_id: UUID,
        task_type: str,
        objective: str,
        hud_profile_name: str | None = None,
        retrieval_focus: str | None = None,
        parent_task_id: UUID | None = None,
        priority: int = 100,
        trigger_type: str | None = None,
        trigger_id: str | None = None,
        source_state_version: int | None = None,
        source_node_id: UUID | None = None,
        source_from_node_id: UUID | None = None,
        source_through_node_id: UUID | None = None,
        source_scene_snapshot_id: UUID | None = None,
        meta: Mapping[str, Any] | None = None,
        execution_mode: str = "auto",
    ) -> CognitiveTask:
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.character_cognitive_task (
                instance_id, parent_task_id, task_type, objective,
                hud_profile_name, retrieval_focus, priority,
                trigger_type, trigger_id, source_state_version, source_node_id,
                source_from_node_id, source_through_node_id, source_scene_snapshot_id, meta,
                execution_mode
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb,$16)
            RETURNING *
            """,
            instance_id,
            parent_task_id,
            task_type,
            objective,
            hud_profile_name,
            retrieval_focus,
            int(priority),
            trigger_type,
            trigger_id,
            source_state_version,
            source_node_id,
            source_from_node_id,
            source_through_node_id,
            source_scene_snapshot_id,
            _json(meta),
            execution_mode,
        )
        return CognitiveTask.from_row(row)

    async def get_task(self, task_id: UUID) -> CognitiveTask | None:
        row = await self.db.fetchrow(
            "SELECT * FROM aios.character_cognitive_task WHERE task_id=$1",
            task_id,
        )
        return CognitiveTask.from_row(row) if row else None

    async def transition_task(
        self,
        task_id: UUID,
        to_status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> CognitiveTask:
        current = await self.get_task(task_id)
        if current is None:
            raise LookupError(f"Unknown cognitive task {task_id}")
        allowed = TASK_TRANSITIONS.get(current.status)
        if allowed is None or to_status not in allowed:
            raise InvalidLifecycleTransition(
                f"Task {task_id}: {current.status} -> {to_status} is not allowed"
            )

        row = await self.db.execute_returning_row(
            """
            UPDATE aios.character_cognitive_task
            SET status=$3,
                started_at=CASE
                    WHEN $3='running' THEN COALESCE(started_at, now())
                    ELSE started_at
                END,
                waiting_at=CASE WHEN $3='waiting' THEN now() ELSE waiting_at END,
                completed_at=CASE
                    WHEN $3 = ANY($4::text[]) THEN now()
                    ELSE completed_at
                END,
                result=CASE WHEN $5::jsonb IS NOT NULL THEN $5::jsonb ELSE result END,
                error=CASE WHEN $6::text IS NOT NULL THEN $6 ELSE error END,
                updated_at=now()
            WHERE task_id=$1 AND status=$2
            RETURNING *
            """,
            task_id,
            current.status,
            to_status,
            list(TASK_TERMINAL),
            json.dumps(dict(result), default=str) if result is not None else None,
            error,
        )
        if not row:
            latest = await self.get_task(task_id)
            raise InvalidLifecycleTransition(
                f"Task {task_id} changed concurrently from {current.status}"
                + (f" to {latest.status}" if latest else "")
            )
        return CognitiveTask.from_row(row)

    async def create_action(
        self,
        *,
        instance_id: UUID,
        action_type: str,
        arguments: Mapping[str, Any] | None = None,
        task_id: UUID | None = None,
        parent_action_id: UUID | None = None,
        side_effect_class: str = "read_only",
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
        proposed_by: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> ActionRecord:
        if task_id is not None:
            task = await self.get_task(task_id)
            if task is None:
                raise LookupError(f"Unknown cognitive task {task_id}")
            if task.instance_id != instance_id:
                raise ValueError(
                    f"Task {task_id} belongs to instance {task.instance_id}, not {instance_id}"
                )
        if parent_action_id is not None:
            parent = await self.get_action(parent_action_id)
            if parent is None:
                raise LookupError(f"Unknown parent action {parent_action_id}")
            if parent.instance_id != instance_id:
                raise ValueError(
                    f"Parent action {parent_action_id} belongs to instance "
                    f"{parent.instance_id}, not {instance_id}"
                )

        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.character_action (
                instance_id, task_id, parent_action_id, action_type, arguments,
                side_effect_class, idempotency_key, expected_state_version,
                proposed_by, meta
            )
            VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10::jsonb)
            ON CONFLICT (instance_id, idempotency_key) DO NOTHING
            RETURNING *
            """,
            instance_id,
            task_id,
            parent_action_id,
            action_type,
            _json(arguments),
            side_effect_class,
            idempotency_key,
            expected_state_version,
            proposed_by,
            _json(meta),
        )
        if row:
            return ActionRecord.from_row(row)
        if idempotency_key is None:
            raise RuntimeError("Action insert failed without an idempotency key conflict")
        existing = await self.db.fetchrow(
            """
            SELECT * FROM aios.character_action
            WHERE instance_id=$1 AND idempotency_key=$2
            """,
            instance_id,
            idempotency_key,
        )
        if not existing:
            raise RuntimeError("Idempotent action exists but could not be loaded")
        return ActionRecord.from_row(existing)

    async def get_action(self, action_id: UUID) -> ActionRecord | None:
        row = await self.db.fetchrow(
            "SELECT * FROM aios.character_action WHERE action_id=$1",
            action_id,
        )
        return ActionRecord.from_row(row) if row else None

    async def transition_action(
        self,
        action_id: UUID,
        to_status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        rejection_reason: str | None = None,
    ) -> ActionRecord:
        current = await self.get_action(action_id)
        if current is None:
            raise LookupError(f"Unknown character action {action_id}")
        allowed = ACTION_TRANSITIONS.get(current.status)
        if allowed is None or to_status not in allowed:
            raise InvalidLifecycleTransition(
                f"Action {action_id}: {current.status} -> {to_status} is not allowed"
            )

        row = await self.db.execute_returning_row(
            """
            UPDATE aios.character_action
            SET status=$3,
                validated_at=CASE
                    WHEN $3='validated' THEN COALESCE(validated_at, now())
                    ELSE validated_at
                END,
                queued_at=CASE
                    WHEN $3='queued' THEN COALESCE(queued_at, now())
                    ELSE queued_at
                END,
                started_at=CASE
                    WHEN $3='running' THEN COALESCE(started_at, now())
                    ELSE started_at
                END,
                completed_at=CASE
                    WHEN $3 = ANY($4::text[]) THEN now()
                    ELSE completed_at
                END,
                result=CASE WHEN $5::jsonb IS NOT NULL THEN $5::jsonb ELSE result END,
                error=CASE WHEN $6::text IS NOT NULL THEN $6 ELSE error END,
                rejection_reason=CASE
                    WHEN $7::text IS NOT NULL THEN $7 ELSE rejection_reason
                END,
                updated_at=now()
            WHERE action_id=$1 AND status=$2
            RETURNING *
            """,
            action_id,
            current.status,
            to_status,
            list(ACTION_TERMINAL),
            json.dumps(dict(result), default=str) if result is not None else None,
            error,
            rejection_reason,
        )
        if not row:
            latest = await self.get_action(action_id)
            raise InvalidLifecycleTransition(
                f"Action {action_id} changed concurrently from {current.status}"
                + (f" to {latest.status}" if latest else "")
            )
        return ActionRecord.from_row(row)

    async def actions_for_task(self, task_id: UUID) -> list[ActionRecord]:
        rows = await self.db.fetch(
            """
            SELECT * FROM aios.character_action
            WHERE task_id=$1
            ORDER BY created_at, action_id
            """,
            task_id,
        )
        return [ActionRecord.from_row(row) for row in rows]
