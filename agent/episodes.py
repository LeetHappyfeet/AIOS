from __future__ import annotations

import logging
from uuid import UUID

from aios_app.db import Database


logger = logging.getLogger(__name__)


class CognitiveEpisodeStore:
    """A host delta is processed only after all admitted branches finish."""

    def __init__(self, db: Database):
        self.db = db

    async def open(self, instance_id: UUID, source_node_id: UUID) -> dict:
        latest=await self.db.fetchrow(
            """SELECT * FROM aios.character_cognitive_episode
               WHERE instance_id=$1 AND source_node_id=$2
               ORDER BY created_at DESC LIMIT 1""",instance_id,source_node_id)
        if latest and latest["status"] == "succeeded":
            return dict(latest)
        row = await self.db.execute_returning_row(
            """INSERT INTO aios.character_cognitive_episode(instance_id,source_node_id,source_timeline_id)
               SELECT $1,node_id,timeline_id FROM aios.dag_node WHERE node_id=$2
               ON CONFLICT(instance_id,source_node_id) WHERE status='active'
               DO UPDATE SET updated_at=aios.character_cognitive_episode.updated_at
               RETURNING *""", instance_id, source_node_id)
        if not row:
            raise ValueError("cognitive episode source node does not exist")
        return dict(row)

    async def seal(self, episode_id: UUID, transaction_id: UUID | None) -> None:
        await self.db.execute(
            """UPDATE aios.character_cognitive_episode
               SET route_transaction_id=$2,sealed_at=now(),updated_at=now()
               WHERE episode_id=$1 AND status='active'""", episode_id, transaction_id)

    async def reconcile(self, episode_id: UUID) -> None:
        async with self.db.atomic():
            episode = await self.db.fetchrow(
                "SELECT * FROM aios.character_cognitive_episode WHERE episode_id=$1 FOR UPDATE",
                episode_id)
            if not episode or episode["status"] != "active" or not episode["sealed_at"]:
                return
            tx = await self.db.fetchrow(
                "SELECT status,reconciled_at FROM aios.internal_cognition_transaction WHERE transaction_id=$1",
                episode["route_transaction_id"]) if episode["route_transaction_id"] else None
            operations = await self.db.fetch(
                "SELECT status,reconciled_at FROM aios.character_cognitive_operation WHERE episode_id=$1",
                episode_id)
            failed = bool(tx and tx["status"] in {"failed","stale","invalid","superseded"})
            failed = failed or any(op["status"] in {"failed","stale"} for op in operations)
            if not failed:
                if tx and (tx["status"] != "consumed" or not tx["reconciled_at"]):
                    return
                if any(op["status"] != "succeeded" or not op["reconciled_at"] for op in operations):
                    return
            await self.db.execute(
                """UPDATE aios.character_cognitive_episode
                   SET status=$2,completed_at=now(),updated_at=now() WHERE episode_id=$1""",
                episode_id, "failed" if failed else "succeeded")
            if failed:
                from .runtime import AgentRuntimeStore
                runtime = await self.db.fetchrow(
                    """SELECT pending_cognitive_source_node_id,pending_cognitive_event_count,cognitive_batch_size
                       FROM aios.character_agent_runtime WHERE instance_id=$1""", episode["instance_id"])
                if (runtime and runtime["pending_cognitive_source_node_id"] == episode["source_node_id"]
                        and runtime["pending_cognitive_event_count"] >= runtime["cognitive_batch_size"]):
                    await AgentRuntimeStore(self.db).rearm_cognitive_delta(
                        instance_id=episode["instance_id"], source_node_id=episode["source_node_id"])
            else:
                from .admission import AutonomyAdmissionService
                await AutonomyAdmissionService(self.db).mark_episode_succeeded(
                    instance_id=episode["instance_id"], through_node_id=episode["source_node_id"])

    async def reconcile_pending(self, limit: int = 100) -> None:
        rows = await self.db.fetch(
            """SELECT episode_id FROM aios.character_cognitive_episode
               WHERE status='active' AND sealed_at IS NOT NULL ORDER BY updated_at LIMIT $1""", limit)
        for row in rows:
            try:
                await self.reconcile(row["episode_id"])
            except Exception:
                logger.exception("Episode reconciliation failed episode=%s",row["episode_id"])
