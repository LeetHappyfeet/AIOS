"""Concrete shared-world runtime for AIOS.

The epistemic RDF world machinery answers what claims belong to which possible
world.  This package answers what entities currently exist in a selected world,
who controls them, and what actions can change their runtime state.
"""

from __future__ import annotations

import asyncio
import logging
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

logger = logging.getLogger("aios.world")

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


async def _coalesce_live_generation(
    db,
    *,
    instance_id: UUID,
    node_id: Optional[UUID],
) -> int:
    """Demote stale queued LIVE work before promoting the current generation.

    Transcript reconciliation advances the source cursor once per historical
    message.  Every intermediate node is briefly the head, so eager promotion
    can otherwise leave hundreds of obsolete LIVE jobs behind.  Running work is
    allowed to finish; only queued work is demoted.  The caller then promotes
    the exact current head dependency chain again.
    """
    if node_id is None:
        return 0
    result = await db.execute(
        """
        UPDATE aios.pipeline_job
        SET priority=GREATEST(priority,35),
            payload=payload - 'live_instance_id',
            updated_at=now()
        WHERE status='queued'
          AND payload->>'live_instance_id'=$1
        """,
        str(instance_id),
    )
    try:
        return int(str(result).split()[-1])
    except (TypeError, ValueError, IndexError):
        return 0


# runtime.py uses its own imported enqueue symbol during character activation.
# Make that activation-side prewarm fire-and-forget; explicit HUD preparation
# below still awaits/promotes the exact requested generation when necessary.
if not getattr(_runtime, "_activation_enqueue_detached_v1", False):
    _original_runtime_enqueue_live_turn_work = _runtime.enqueue_live_turn_work
    _detached_activation_tasks: set[asyncio.Task] = set()

    async def _run_activation_live_work(*, db, instance_id: UUID, node_id: Optional[UUID]) -> None:
        await _coalesce_live_generation(
            db,
            instance_id=instance_id,
            node_id=node_id,
        )
        await _original_runtime_enqueue_live_turn_work(
            db,
            instance_id=instance_id,
            node_id=node_id,
        )

    async def _enqueue_activation_live_work_detached(
        db,
        *,
        instance_id: UUID,
        node_id: Optional[UUID],
    ) -> int:
        task = asyncio.create_task(
            _run_activation_live_work(
                db=db,
                instance_id=instance_id,
                node_id=node_id,
            )
        )
        _detached_activation_tasks.add(task)

        def _finished(done: asyncio.Task) -> None:
            _detached_activation_tasks.discard(done)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error(
                    "Detached activation live-work failed instance=%s node=%s: %s",
                    instance_id,
                    node_id,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_finished)
        return 0

    _runtime.enqueue_live_turn_work = _enqueue_activation_live_work_detached
    _runtime._activation_enqueue_detached_v1 = True


if not getattr(WorldRuntimeService, "_live_readiness_v3", False):
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

        # Activation is the only production caller that asks for an unbounded
        # current-head prewarm with wait_ms=0.  Do not make the control-plane
        # activation response wait for semantic/HUD work.  The explicit HUD
        # endpoint will still drive the readiness barrier if this background
        # prewarm has not finished yet.
        if current_target and through_node_id is None and wait_ms == 0:
            await _coalesce_live_generation(
                self.db,
                instance_id=instance_id,
                node_id=target_node_id,
            )
            self._spawn_background(
                _prepare_frame_with_live_readiness(
                    self,
                    instance_id,
                    through_node_id=target_node_id,
                    recent_limit=recent_limit,
                    token_budget=token_budget,
                    wait_ms=1200,
                ),
                label=f"activation-hud-prewarm:{instance_id}:{target_node_id}",
            )
            return {
                "presence": {
                    "instance_id": instance_id,
                    "source_head_node_id": target_node_id,
                    "state_version": state.get("state_version"),
                },
                "hud": {
                    "generation_ready": False,
                    "cache": "activation-prewarm-deferred",
                    "freshness": {
                        "source_head_node_id": str(target_node_id) if target_node_id else None,
                        "requested_source_node_id": str(target_node_id) if target_node_id else None,
                        "source_current": True,
                        "runtime_current": True,
                        "semantic_ready": False,
                        "char_ready": False,
                    },
                },
            }

        # Historical replay keeps its immutable prepared snapshot semantics.
        # Only the current live head participates in the readiness barrier.
        live_ready = True
        if current_target:
            # Coalesce stale reconciliation generations once, then promote only
            # the dependency chain for the exact head this HUD request targets.
            await _coalesce_live_generation(
                self.db,
                instance_id=instance_id,
                node_id=target_node_id,
            )
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
    WorldRuntimeService._live_readiness_v3 = True


__all__ = ["WorldRuntimeService", "RuntimeConflict", "RuntimeNotFound"]
