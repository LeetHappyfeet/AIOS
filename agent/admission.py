from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.hud.context import HUDContextResolver
from .runtime import AgentRuntimeStore


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str
    instance_id: UUID
    source_from_node_id: UUID | None = None
    source_through_node_id: UUID | None = None
    source_state_version: int | None = None
    host_cognition: bool = False
    pending_event_count: int = 0


class AutonomyAdmissionService:
    """Cheap boundary between experience and expensive background cognition.

    Ordinary host-driven chat accumulates a cognitive delta. It does not invoke
    inference per message. Once enough genuinely new source experience has
    accumulated, one deduplicated wake represents the whole pending episode.
    Explicit/external events may still wake immediately through AgentRuntimeStore.
    """

    def __init__(self, db: Database):
        self.db = db
        self.runtime = AgentRuntimeStore(db)
        self.contexts = HUDContextResolver(db)

    async def observe_host_experience(
        self,
        *,
        instance_id: UUID,
        source_node_id: UUID,
        source_event_id: int | None = None,
        source: str = "host",
    ) -> AdmissionDecision:
        context = await self.contexts.resolve(instance_id)
        await self.runtime.ensure(instance_id)
        row = await self.db.execute_returning_row(
            """
            UPDATE aios.character_agent_runtime
            SET pending_cognitive_source_node_id=$2,
                pending_cognitive_event_count=pending_cognitive_event_count + 1,
                last_activity_at=now(), updated_at=now()
            WHERE instance_id=$1
            RETURNING last_cognitive_source_node_id, pending_cognitive_event_count,
                      cognitive_batch_size
            """,
            instance_id, source_node_id,
        )
        count = int(row["pending_cognitive_event_count"])
        batch = int(row["cognitive_batch_size"])
        start = row["last_cognitive_source_node_id"]
        if count < batch:
            return AdmissionDecision(
                False, "host_delta_accumulating", instance_id, start, source_node_id,
                context.state_version, True, count,
            )

        wake_id = await self.runtime.wake(
            instance_id=instance_id,
            event_type="COGNITIVE_DELTA_READY",
            source_type="dag_node",
            source_id=str(source_node_id),
            payload={
                "source_from_node_id": str(start) if start else None,
                "source_through_node_id": str(source_node_id),
                "source_state_version": context.state_version,
                "host_cognition": True,
                "source": source,
                "source_event_id": source_event_id,
                "pending_event_count": count,
            },
            priority=150,
            dedupe_key=f"cognitive-delta:{source_node_id}",
        )
        return AdmissionDecision(
            True, f"host_delta_batch:{wake_id}", instance_id, start, source_node_id,
            context.state_version, True, count,
        )

    async def mark_episode_succeeded(
        self, *, instance_id: UUID, through_node_id: UUID | None
    ) -> None:
        if through_node_id is None:
            return
        await self.db.execute(
            """
            UPDATE aios.character_agent_runtime
            SET last_cognitive_source_node_id=$2,
                pending_cognitive_event_count=CASE
                    WHEN pending_cognitive_source_node_id=$2 THEN 0
                    ELSE pending_cognitive_event_count
                END,
                pending_cognitive_source_node_id=CASE
                    WHEN pending_cognitive_source_node_id=$2 THEN NULL
                    ELSE pending_cognitive_source_node_id
                END,
                updated_at=now()
            WHERE instance_id=$1
            """,
            instance_id, through_node_id,
        )
