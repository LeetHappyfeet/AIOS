"""Concrete shared-world runtime for AIOS.

The epistemic RDF world machinery answers what claims belong to which possible
world.  This package answers what entities currently exist in a selected world,
who controls them, and what actions can change their runtime state.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional
from uuid import UUID

from aios_app.hud.readiness import (
    enqueue_live_turn_work,
    readiness_state,
    set_retrieval_ready,
    source_node_retrieval_ready,
    source_node_topology_ready,
)

from . import runtime as _runtime

WorldRuntimeService = _runtime.WorldRuntimeService
RuntimeConflict = _runtime.RuntimeConflict
RuntimeNotFound = _runtime.RuntimeNotFound


async def _invalidate_current_snapshot(service, instance_id: UUID, node_id: Optional[UUID]) -> None:
    if node_id is None:
        return
    await service.db.execute(
        """
        UPDATE aios.character_hud_readiness
        SET status='dirty',
            prepared_source_node_id=NULL,
            prepared_source_event_id=NULL,
            prepared_state_version=NULL,
            prepared_at=NULL,
            hud_json=NULL,
            hud_text=NULL,
            dirty_since=COALESCE(dirty_since,now()),
            updated_at=now()
        WHERE instance_id=$1
          AND prepared_source_node_id=$2
        """,
        instance_id,
        node_id,
    )


if not getattr(WorldRuntimeService, "_live_readiness_v2", False):
    _original_prepare_frame = WorldRuntimeService.prepare_frame

    async def _prepare_frame_with_live_readiness(
        self,
        instance_id: UUID,
        *,
        through_node_id: Optional[UUID] = None,
        recent_limit: Optional[int] = None,
        token_budget: Optional[int] = None,
        wait_ms: int = 1200,
    ):
        state = await self.get_state(instance_id)
        target_node_id = through_node_id or state.get("source_head_node_id")
        current_target = target_node_id == state.get("source_head_node_id")

        # Historical replay keeps its immutable prepared snapshot semantics.
        # Only the current live head participates in the readiness barrier.
        live_ready = True
        if current_target:
            live_ready = await source_node_retrieval_ready(
                self.db,
                instance_id=instance_id,
                node_id=target_node_id,
            )
            if not live_ready:
                await enqueue_live_turn_work(
                    self.db,
                    instance_id=instance_id,
                    node_id=target_node_id,
                )

            deadline = time.monotonic() + max(0, wait_ms) / 1000.0
            next_promote = time.monotonic() + 0.25
            while not live_ready and wait_ms > 0 and time.monotonic() < deadline:
                await asyncio.sleep(min(0.10, max(0.0, deadline - time.monotonic())))
                live_ready = await source_node_retrieval_ready(
                    self.db,
                    instance_id=instance_id,
                    node_id=target_node_id,
                )
                now = time.monotonic()
                if not live_ready and now >= next_promote:
                    await enqueue_live_turn_work(
                        self.db,
                        instance_id=instance_id,
                        node_id=target_node_id,
                    )
                    next_promote = now + 0.25

            ready_before = await readiness_state(self.db, instance_id=instance_id)
            cached_current = ready_before.get("prepared_source_node_id") == target_node_id
            cached_before_live_barrier = (
                cached_current
                and ready_before.get("retrieval_ready_node_id") != target_node_id
            )
            if cached_current and (not live_ready or cached_before_live_barrier):
                await _invalidate_current_snapshot(self, instance_id, target_node_id)
            if live_ready:
                await set_retrieval_ready(
                    self.db,
                    instance_id=instance_id,
                    node_id=target_node_id,
                )

        frame = await _original_prepare_frame(
            self,
            instance_id,
            through_node_id=through_node_id,
            recent_limit=recent_limit,
            token_budget=token_budget,
            wait_ms=0,
        )

        if not current_target:
            return frame

        live_ready = await source_node_retrieval_ready(
            self.db,
            instance_id=instance_id,
            node_id=target_node_id,
        )
        topology_ready = await source_node_topology_ready(
            self.db,
            instance_id=instance_id,
            node_id=target_node_id,
        )

        hud = frame.setdefault("hud", {})
        hud["generation_ready"] = bool(hud.get("generation_ready")) and live_ready
        freshness = hud.setdefault("freshness", {})
        freshness["semantic_ready"] = live_ready
        freshness["char_ready"] = live_ready
        freshness["topology_current"] = topology_ready

        if hud["generation_ready"]:
            await set_retrieval_ready(
                self.db,
                instance_id=instance_id,
                node_id=target_node_id,
            )
        else:
            # The legacy runtime could persist a stable-coordinate HUD before
            # semantic completion. Remove that snapshot so it cannot become an
            # exact-cache hit on the next request.
            await _invalidate_current_snapshot(self, instance_id, target_node_id)

        return frame

    WorldRuntimeService.prepare_frame = _prepare_frame_with_live_readiness
    WorldRuntimeService._live_readiness_v2 = True


__all__ = ["WorldRuntimeService", "RuntimeConflict", "RuntimeNotFound"]
