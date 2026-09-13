"""Concrete shared-world runtime for AIOS.

The epistemic RDF world machinery answers what claims belong to which possible
world.  This package answers what entities currently exist in a selected world,
who controls them, and what actions can change their runtime state.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from typing import Optional
from uuid import UUID

from aios_app.hud import readiness as _readiness
from aios_app.hud.profile import get_profile as get_hud_profile
from aios_app.hud.relevance import HUDRelevanceScorer
from aios_app.hud.readiness import (
    enqueue_live_turn_work,
    readiness_state,
    set_retrieval_ready,
    source_node_retrieval_ready,
    source_node_topology_ready,
)
from aios_app.plugins.types import PluginRuntimeContext

from . import runtime as _runtime
from .source_cursor import advance_matching_runtime_source_cursor

logger = logging.getLogger("aios.world")

WorldRuntimeService = _runtime.WorldRuntimeService
RuntimeConflict = _runtime.RuntimeConflict
RuntimeNotFound = _runtime.RuntimeNotFound

RETRIEVAL_PREWARM_DEBOUNCE_SECONDS = 0.12
_runtime_services: weakref.WeakSet = weakref.WeakSet()


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


# Keep track of runtime services in this process so source-cursor ingestion can
# warm the exact HUD cognition service that will serve the later request.  This
# avoids a second cache or another worker process just for speculation.
if not getattr(WorldRuntimeService, "_retrieval_prewarm_registration_v1", False):
    _original_runtime_init = WorldRuntimeService.__init__

    def _registered_runtime_init(self, *args, **kwargs):
        _original_runtime_init(self, *args, **kwargs)
        self._retrieval_prewarm_tasks = {}
        _runtime_services.add(self)

    async def _run_retrieval_prewarm(
        self,
        *,
        instance_id: UUID,
        source_head_node_id: UUID,
    ) -> None:
        # A tiny debounce collapses transcript replay/reconciliation bursts. The
        # next source head cancels this task before expensive retrieval begins.
        await asyncio.sleep(RETRIEVAL_PREWARM_DEBOUNCE_SECONDS)

        state = await self.get_state(instance_id)
        if state.get("source_head_node_id") != source_head_node_id:
            return

        context = await self.hud.context_resolver.resolve(instance_id)
        if context.source_head_node_id != source_head_node_id:
            return

        raw_state = await self.hud._runtime_state(instance_id)
        plugin_snapshot = await self.hud.plugin_manager.collect(
            PluginRuntimeContext(
                instance_id=context.instance_id,
                character_id=context.character_id,
                entity_id=context.entity_id,
                world_id=context.world_id,
                world_key=context.world_key,
                timeline_id=context.timeline_id,
                location_entity_id=context.location_entity_id,
                raw_state=raw_state,
            )
        )
        hud_profile = await get_hud_profile(
            self.db,
            character_id=context.character_id,
        )
        attention = await self.hud.cognition.resolve_attention_inputs(
            context,
            raw_state,
            plugin_snapshot,
            recent_limit=hud_profile.recent_event_limit,
        )
        scorer = HUDRelevanceScorer(
            context,
            focus_text=attention.retrieval_focus_text,
            goals=attention.goals,
        )

        started = time.perf_counter()
        await self.hud.cognition.prepare_retrieval(
            context,
            scorer,
            attention,
            hud_profile,
        )
        after = await self.get_state(instance_id)
        if after.get("source_head_node_id") != source_head_node_id:
            logger.debug(
                "Discarding completed retrieval prewarm for stale head instance=%s node=%s",
                instance_id,
                source_head_node_id,
            )
            return

        logger.debug(
            "Retrieval prewarm ready instance=%s node=%s elapsed_ms=%.1f",
            instance_id,
            source_head_node_id,
            (time.perf_counter() - started) * 1000.0,
        )

    def _schedule_retrieval_prewarm(
        self,
        *,
        instance_id: UUID,
        source_head_node_id: Optional[UUID],
    ) -> None:
        if source_head_node_id is None:
            return

        prior = self._retrieval_prewarm_tasks.get(instance_id)
        if prior is not None and not prior.done():
            prior.cancel()

        task = asyncio.create_task(
            _run_retrieval_prewarm(
                self,
                instance_id=instance_id,
                source_head_node_id=source_head_node_id,
            )
        )
        self._retrieval_prewarm_tasks[instance_id] = task
        self._background_tasks.add(task)

        def _finished(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            if self._retrieval_prewarm_tasks.get(instance_id) is done:
                self._retrieval_prewarm_tasks.pop(instance_id, None)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error(
                    "Retrieval prewarm failed instance=%s node=%s: %s",
                    instance_id,
                    source_head_node_id,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_finished)

    WorldRuntimeService.__init__ = _registered_runtime_init
    WorldRuntimeService.schedule_retrieval_prewarm = _schedule_retrieval_prewarm
    WorldRuntimeService._retrieval_prewarm_registration_v1 = True


# Ingest must advance the exact source cursor and dirty the runtime, but it must
# not promote every transient reconciliation head into LIVE semantic work.  It
# may, however, stage established-memory retrieval.  Latest-head cancellation
# plus the debounce above prevents historical reconciliation from becoming a
# second semantic backlog.
if not getattr(_readiness, "_head_only_dirty_v1", False):
    async def _mark_matching_runtime_dirty_head_only(
        db,
        *,
        character_id: str,
        session_id: Optional[UUID],
        user_name: Optional[str],
        scope_key: str,
        source_timeline_id: UUID,
        source_head_node_id: UUID,
        source_head_event_id: int,
    ) -> list[UUID]:
        instance_ids = await advance_matching_runtime_source_cursor(
            db,
            character_id=character_id,
            session_id=session_id,
            user_name=user_name,
            scope_key=scope_key,
            source_timeline_id=source_timeline_id,
            source_head_node_id=source_head_node_id,
            source_head_event_id=source_head_event_id,
        )
        for instance_id in instance_ids:
            await _readiness.mark_source_dirty(
                db,
                instance_id=instance_id,
                source_timeline_id=source_timeline_id,
                source_head_node_id=source_head_node_id,
                source_head_event_id=source_head_event_id,
            )
            for service in list(_runtime_services):
                if getattr(service, "db", None) is db:
                    service.schedule_retrieval_prewarm(
                        instance_id=instance_id,
                        source_head_node_id=source_head_node_id,
                    )
        return list(instance_ids)

    _readiness.mark_matching_runtime_dirty = _mark_matching_runtime_dirty_head_only
    _readiness._head_only_dirty_v1 = True


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
