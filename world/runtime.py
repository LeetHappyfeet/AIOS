from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, Optional
from uuid import UUID

from aios_app.causal import CausalIntegrityKernel, CausalRejected
from aios_app.causal.types import CausalCandidate
from aios_app.db import Database
from aios_app.dag import get_or_create_timeline, add_node_and_edge
from aios_app.hud.frame import HUDAssembler
from aios_app.hud.render_text import render_hud_text
from aios_app.hud.singleflight import AsyncSingleFlight
from aios_app.hud.readiness import (
    ensure_readiness_row,
    enqueue_live_turn_work,
    readiness_state,
    save_prepared_snapshot,
    set_retrieval_ready,
    source_node_retrieval_ready,
)
from aios_app.world.topology import (
    ensure_character_root_world,
    ensure_runtime_branch_world,
    latest_source_anchor,
)

logger = logging.getLogger("aios.world.runtime")


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("expected JSON object")
        return decoded
    return dict(value)


def _subjective_runtime_state(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return only runtime coordination and /char-owned state.

    Deterministic values such as location/health/energy are intentionally not
    copied into the HUD surface. Objective location is projected through
    /world located_in relations; future hard-state domains follow the same path.
    """
    keep = {
        "instance_id",
        "world_id",
        "timeline_id",
        "head_node_id",
        "source_timeline_id",
        "source_head_node_id",
        "lifecycle_state",
        "emotional_state",
        "social_state",
        "goals",
        "active_tasks",
        "runtime_flags",
        "state_version",
        "updated_at",
        "character_id",
        "entity_id",
        "entity_type",
        "display_name",
        "world_key",
    }
    return {key: value for key, value in row.items() if key in keep}


def _sanitize_hud_frame(frame: Dict[str, Any]) -> Dict[str, Any]:
    """Final firewall preventing raw deterministic compatibility state in HUD."""
    state = frame.get("state")
    if isinstance(state, dict):
        for key in ("health", "stamina", "energy", "physical"):
            state.pop(key, None)

    # world_rule is deterministic policy. Only rules explicitly acquired as
    # character knowledge belong in /char/HUD.
    rules = frame.get("rules")
    if isinstance(rules, list):
        frame["rules"] = [
            rule
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("source") == "character_knowledge"
        ]
    return frame


class RuntimeConflict(RuntimeError):
    """Raised when a caller acts on a stale state_version."""


class RuntimeNotFound(RuntimeError):
    """Raised when a runtime instance/entity cannot be resolved."""


@dataclass(frozen=True)
class ActivationResult:
    character_id: str
    instance_id: UUID
    entity_id: UUID
    world_id: UUID
    timeline_id: UUID
    head_node_id: Optional[UUID]
    state_version: int
    lifecycle_state: str


class WorldRuntimeService:
    """Runtime coordinator above the objective causal kernel and /char HUD.

    This service no longer owns deterministic mutation policy. It records
    controller intent/DAG history, asks CausalIntegrityKernel to validate and
    commit objective transitions, and keeps HUD generation coordinates current.
    """

    def __init__(self, db: Database):
        self.db = db
        self.causal = CausalIntegrityKernel(db)
        self.hud = HUDAssembler(db)
        self._hud_builds: AsyncSingleFlight[
            tuple[UUID, Optional[UUID], int, Optional[int], Optional[int]],
            Dict[str, Any],
        ] = AsyncSingleFlight()
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_background(self, awaitable: Awaitable[Any], *, label: str) -> None:
        task = asyncio.create_task(awaitable)
        self._background_tasks.add(task)

        def _finished(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error(
                    "Background runtime task failed [%s]: %s",
                    label,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_finished)

    async def activate_character(
        self,
        *,
        character_id: str,
        user_name: str,
        session_id: Optional[UUID],
        scope_key: str,
        world_id: Optional[UUID] = None,
        world_key: Optional[str] = None,
        controller_type: str = "agent",
        controller_ref: Optional[str] = None,
    ) -> ActivationResult:
        ident = await self.db.fetchrow(
            "SELECT character_id, display_name, home_world_id FROM aios.character_identity WHERE character_id=$1",
            character_id,
        )
        if not ident:
            raise RuntimeNotFound(f"Unknown character_id '{character_id}'")

        if world_id is not None or world_key is not None:
            world = await self._resolve_world(
                world_id=world_id,
                world_key=world_key,
                home_world_id=ident["home_world_id"],
            )
        else:
            root_world_id = await ensure_character_root_world(
                self.db,
                character_id=character_id,
            )
            world = await ensure_runtime_branch_world(
                self.db,
                character_id=character_id,
                session_id=session_id,
                root_world_id=root_world_id,
            )
        world_id = world["world_id"]

        instance = await self.db.fetchrow(
            """
            SELECT instance_id
            FROM aios.character_instance
            WHERE character_id=$1
              AND COALESCE(current_world_id, world_id)=$2
              AND meta->>'runtime_user_name'=$3
            ORDER BY created_at DESC
            LIMIT 1
            """,
            character_id,
            world_id,
            user_name,
        )
        if not instance:
            instance = await self.db.execute_returning_row(
                """
                INSERT INTO aios.character_instance (
                    character_id, world_id, current_world_id, meta
                )
                VALUES ($1,$2,$2,jsonb_build_object('runtime_user_name',$3::text))
                RETURNING instance_id
                """,
                character_id,
                world_id,
                user_name,
            )
        instance_id = instance["instance_id"]

        timeline_id = await get_or_create_timeline(
            self.db,
            world_key=world["world_key"],
            session_id=session_id,
            character_id=character_id,
            user_name=user_name,
            scope_key=scope_key,
            meta={
                "runtime_instance_id": str(instance_id),
                "world_runtime": True,
            },
        )

        entity = await self.db.fetchrow(
            "SELECT entity_id FROM aios.world_entity WHERE character_instance_id=$1 LIMIT 1",
            instance_id,
        )
        if not entity:
            entity = await self.db.execute_returning_row(
                """
                INSERT INTO aios.world_entity (
                    world_id, entity_key, entity_type, display_name,
                    character_instance_id, meta
                )
                VALUES ($1,$2,'character',$3,$4,'{}'::jsonb)
                RETURNING entity_id
                """,
                world_id,
                f"character-instance:{instance_id}",
                ident["display_name"] or character_id,
                instance_id,
            )
        entity_id = entity["entity_id"]

        controller_ref = controller_ref or (
            user_name if controller_type == "human" else f"character:{character_id}"
        )
        if controller_ref:
            await self.db.execute(
                """
                INSERT INTO aios.entity_controller (
                    entity_id, controller_type, controller_ref, authority
                )
                VALUES ($1,$2,$3,'primary')
                ON CONFLICT (entity_id, controller_type, controller_ref)
                DO UPDATE SET active=true
                """,
                entity_id,
                controller_type,
                controller_ref,
            )

        head = await self.db.fetchrow(
            """
            SELECT node_id
            FROM aios.dag_node
            WHERE timeline_id=$1
            ORDER BY event_id DESC
            LIMIT 1
            """,
            timeline_id,
        )
        head_node_id = head["node_id"] if head else None

        source_timeline_id = world.get("anchor_timeline_id")
        source_head_node_id = world.get("anchor_node_id")
        if session_id is not None:
            latest_timeline_id, latest_node_id = await latest_source_anchor(
                self.db,
                character_id=character_id,
                session_id=session_id,
            )
            if latest_timeline_id is not None:
                source_timeline_id = latest_timeline_id
            if latest_node_id is not None:
                source_head_node_id = latest_node_id

        await self.db.execute(
            """
            INSERT INTO aios.character_runtime_state (
                instance_id, world_id, timeline_id, head_node_id,
                source_timeline_id, source_head_node_id, lifecycle_state
            )
            VALUES ($1,$2,$3,$4,$5,$6,'ready')
            ON CONFLICT (instance_id) DO UPDATE
            SET world_id=EXCLUDED.world_id,
                timeline_id=EXCLUDED.timeline_id,
                head_node_id=COALESCE(EXCLUDED.head_node_id, aios.character_runtime_state.head_node_id),
                source_timeline_id=COALESCE(
                    EXCLUDED.source_timeline_id,
                    aios.character_runtime_state.source_timeline_id
                ),
                source_head_node_id=COALESCE(
                    EXCLUDED.source_head_node_id,
                    aios.character_runtime_state.source_head_node_id
                ),
                lifecycle_state='ready',
                updated_at=now()
            """,
            instance_id,
            world_id,
            timeline_id,
            head_node_id,
            source_timeline_id,
            source_head_node_id,
        )
        try:
            await ensure_readiness_row(self.db, instance_id=instance_id, live=True)
            if await source_node_retrieval_ready(
                self.db,
                instance_id=instance_id,
                node_id=source_head_node_id,
            ):
                await self.prepare_frame(instance_id, wait_ms=0)
            else:
                await enqueue_live_turn_work(
                    self.db,
                    instance_id=instance_id,
                    node_id=source_head_node_id,
                )
        except Exception:
            logger.exception("Failed to prewarm HUD for live instance %s", instance_id)

        return await self._activation_result(instance_id)

    async def get_state(self, instance_id: UUID) -> Dict[str, Any]:
        row = await self.db.fetchrow(
            """
            SELECT
                rs.*, ci.character_id, we.entity_id, we.entity_type,
                we.display_name, w.world_key,
                loc.object_entity_id AS objective_location_entity_id
            FROM aios.character_runtime_state rs
            JOIN aios.character_instance ci ON ci.instance_id=rs.instance_id
            JOIN aios.world w ON w.world_id=rs.world_id
            LEFT JOIN aios.world_entity we ON we.character_instance_id=rs.instance_id
            LEFT JOIN LATERAL (
                SELECT r.object_entity_id
                FROM aios.world_entity_relation r
                WHERE r.world_id=rs.world_id
                  AND r.subject_entity_id=we.entity_id
                  AND r.relation_type='located_in'
                  AND r.valid_to_node_id IS NULL
                ORDER BY r.created_at DESC
                LIMIT 1
            ) loc ON true
            WHERE rs.instance_id=$1
            """,
            instance_id,
        )
        if not row:
            raise RuntimeNotFound(f"Unknown runtime instance {instance_id}")
        state = dict(row)
        # Compatibility key now comes from /world projection, never from the
        # legacy mutable column.
        state["location_entity_id"] = state.pop("objective_location_entity_id", None)
        return state

    async def prepare_frame(
        self,
        instance_id: UUID,
        *,
        through_node_id: Optional[UUID] = None,
        recent_limit: Optional[int] = None,
        token_budget: Optional[int] = None,
        wait_ms: int = 1200,
    ) -> Dict[str, Any]:
        request_started = time.perf_counter()
        await ensure_readiness_row(self.db, instance_id=instance_id, live=True)
        state = await self.get_state(instance_id)
        target_node_id = through_node_id or state.get("source_head_node_id")
        ready = await readiness_state(self.db, instance_id=instance_id)

        replay_cached = (
            through_node_id is not None
            and through_node_id != state.get("source_head_node_id")
            and ready.get("prepared_source_node_id") == through_node_id
            and ready.get("prepared_state_version") == state.get("state_version")
            and ready.get("hud_json") is not None
        )
        if replay_cached:
            frame = _sanitize_hud_frame(_json_object(ready["hud_json"]))
            frame.setdefault("hud", {})["cache"] = "replayed"
            freshness = frame["hud"].setdefault("freshness", {})
            freshness["replayed_snapshot"] = True
            freshness["active_source_head_node_id"] = (
                str(state["source_head_node_id"])
                if state.get("source_head_node_id") else None
            )
            freshness["requested_source_node_id"] = str(through_node_id)
            return frame

        if through_node_id is not None:
            target = await self.db.fetchrow(
                "SELECT timeline_id, event_id FROM aios.dag_node WHERE node_id=$1",
                through_node_id,
            )
            head = None
            if state.get("source_head_node_id"):
                head = await self.db.fetchrow(
                    "SELECT timeline_id, event_id FROM aios.dag_node WHERE node_id=$1",
                    state["source_head_node_id"],
                )
            if not target:
                raise RuntimeNotFound(f"Unknown source node {through_node_id}")
            if (
                not head
                or target["timeline_id"] != state.get("source_timeline_id")
                or target["event_id"] != head["event_id"]
                or through_node_id != state.get("source_head_node_id")
            ):
                raise RuntimeConflict(
                    "requested preparation node is not the current active source head"
                )

        exact_cached = (
            ready.get("status") == "ready"
            and ready.get("prepared_source_node_id") == target_node_id
            and ready.get("prepared_state_version") == state.get("state_version")
            and ready.get("hud_json") is not None
        )
        if exact_cached:
            frame = _sanitize_hud_frame(_json_object(ready["hud_json"]))
            frame.setdefault("hud", {})["cache"] = "prepared"
            return frame

        wait_budget_ms = max(0, min(int(wait_ms), 10000))
        deadline = time.monotonic() + (wait_budget_ms / 1000.0)
        semantic_current = await source_node_retrieval_ready(
            self.db,
            instance_id=instance_id,
            node_id=target_node_id,
        )
        if not semantic_current:
            await enqueue_live_turn_work(
                self.db,
                instance_id=instance_id,
                node_id=target_node_id,
            )

        while not semantic_current and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))
            semantic_current = await source_node_retrieval_ready(
                self.db,
                instance_id=instance_id,
                node_id=target_node_id,
            )
            if not semantic_current:
                await enqueue_live_turn_work(
                    self.db,
                    instance_id=instance_id,
                    node_id=target_node_id,
                )

        if not semantic_current:
            after = await self.get_state(instance_id)
            coordinates_stable = (
                after.get("state_version") == state.get("state_version")
                and after.get("source_head_node_id") == state.get("source_head_node_id")
            )
            await self.db.execute(
                """
                UPDATE aios.character_hud_readiness
                SET status='dirty',
                    last_error='retrieval substrate not ready before HUD wait budget expired',
                    updated_at=now()
                WHERE instance_id=$1
                """,
                instance_id,
            )
            frame = _sanitize_hud_frame({
                "identity": {
                    "character_id": after.get("character_id"),
                    "display_name": after.get("display_name") or after.get("character_id"),
                },
                "presence": {
                    "instance_id": after.get("instance_id"),
                    "world_id": after.get("world_id"),
                    "world_key": after.get("world_key"),
                    "timeline_id": after.get("timeline_id"),
                    "state_version": after.get("state_version"),
                    "location_entity_id": after.get("location_entity_id"),
                },
                "state": _subjective_runtime_state(after),
                "hud": {
                    "generation_ready": False,
                    "cache": "not_ready",
                    "wait_exhausted": True,
                    "wait_budget_ms": wait_budget_ms,
                    "freshness": {
                        "runtime_state_version": after.get("state_version"),
                        "source_head_node_id": str(after["source_head_node_id"])
                            if after.get("source_head_node_id") else None,
                        "requested_source_node_id": str(target_node_id)
                            if target_node_id else None,
                        "retrieval_ready_node_id": None,
                        "source_current": after.get("source_head_node_id") == target_node_id,
                        "runtime_current": coordinates_stable,
                        "topology_current": False,
                    },
                },
            })
            logger.info(
                "HUD wait expired instance=%s node=%s wait_ms=%d total_request_ms=%.1f; "
                "returning generation_ready=false without topology traversal",
                instance_id,
                target_node_id,
                wait_budget_ms,
                (time.perf_counter() - request_started) * 1000.0,
            )
            return frame

        await set_retrieval_ready(
            self.db,
            instance_id=instance_id,
            node_id=target_node_id,
        )

        build_key = (
            instance_id,
            target_node_id,
            int(state.get("state_version") or 0),
            recent_limit,
            token_budget,
        )
        frame = await self._hud_builds.run(
            build_key,
            lambda: self._build_generation_frame(
                instance_id=instance_id,
                state=state,
                target_node_id=target_node_id,
                recent_limit=recent_limit,
                token_budget=token_budget,
            ),
        )
        logger.debug(
            "HUD request instance=%s node=%s wait_ms=%d total_request_ms=%.1f active_builds=%d",
            instance_id,
            target_node_id,
            wait_budget_ms,
            (time.perf_counter() - request_started) * 1000.0,
            self._hud_builds.active(),
        )
        return frame

    async def _build_generation_frame(
        self,
        *,
        instance_id: UUID,
        state: Dict[str, Any],
        target_node_id: Optional[UUID],
        recent_limit: Optional[int],
        token_budget: Optional[int],
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        timings: dict[str, float] = {}

        status_started = time.perf_counter()
        await self.db.execute(
            """
            UPDATE aios.character_hud_readiness
            SET status='preparing', last_error=NULL, updated_at=now()
            WHERE instance_id=$1
            """,
            instance_id,
        )
        timings["status"] = (time.perf_counter() - status_started) * 1000.0

        semantic_started = time.perf_counter()
        semantic_current = await source_node_retrieval_ready(
            self.db,
            instance_id=instance_id,
            node_id=target_node_id,
        )
        timings["semantic_check"] = (time.perf_counter() - semantic_started) * 1000.0

        readiness_started = time.perf_counter()
        if semantic_current:
            await set_retrieval_ready(
                self.db,
                instance_id=instance_id,
                node_id=target_node_id,
            )
        else:
            self._spawn_background(
                enqueue_live_turn_work(
                    self.db,
                    instance_id=instance_id,
                    node_id=target_node_id,
                ),
                label=f"live-enrichment:{instance_id}:{target_node_id}",
            )
        timings["readiness_update"] = (time.perf_counter() - readiness_started) * 1000.0

        hud_started = time.perf_counter()
        try:
            frame = await self.hud.build(
                instance_id,
                recent_limit=recent_limit,
                token_budget=token_budget,
            )
            frame = _sanitize_hud_frame(frame)
        except LookupError as exc:
            raise RuntimeNotFound(str(exc)) from exc
        except Exception:
            logger.exception(
                "HUD assembly failed instance=%s node=%s elapsed_ms=%.1f",
                instance_id,
                target_node_id,
                (time.perf_counter() - started) * 1000.0,
            )
            raise
        timings["hud_build"] = (time.perf_counter() - hud_started) * 1000.0

        state_started = time.perf_counter()
        after = await self.get_state(instance_id)
        timings["coordinate_recheck"] = (time.perf_counter() - state_started) * 1000.0
        coordinates_stable = (
            after.get("state_version") == state.get("state_version")
            and after.get("source_head_node_id") == state.get("source_head_node_id")
        )
        generation_ready = bool(
            semantic_current
            and coordinates_stable
            and after.get("source_head_node_id") == target_node_id
        )
        frame.setdefault("hud", {})
        frame["hud"]["generation_ready"] = generation_ready
        frame["hud"]["freshness"] = {
            "runtime_state_version": after.get("state_version"),
            "source_head_node_id": str(after["source_head_node_id"])
                if after.get("source_head_node_id") else None,
            "requested_source_node_id": str(target_node_id) if target_node_id else None,
            "retrieval_ready_node_id": str(target_node_id)
                if semantic_current and target_node_id else None,
            "source_current": after.get("source_head_node_id") == target_node_id,
            "runtime_current": coordinates_stable,
            "topology_current": semantic_current,
        }
        frame["hud"]["cache"] = "rebuilt"

        snapshot_started = time.perf_counter()
        if generation_ready:
            text = render_hud_text(frame)
            await save_prepared_snapshot(
                self.db,
                instance_id=instance_id,
                source_node_id=target_node_id,
                state_version=int(after["state_version"]),
                hud_json=frame,
                hud_text=text,
            )
        else:
            await self.db.execute(
                """
                UPDATE aios.character_hud_readiness
                SET status='dirty',
                    last_error=CASE
                        WHEN $2 THEN 'runtime/source coordinates changed during HUD assembly'
                        WHEN NOT $3 THEN 'retrieval substrate became stale during HUD assembly'
                        ELSE 'HUD snapshot was not generation-consistent'
                    END,
                    updated_at=now()
                WHERE instance_id=$1
                """,
                instance_id,
                not coordinates_stable,
                semantic_current,
            )
        timings["snapshot"] = (time.perf_counter() - snapshot_started) * 1000.0
        total_ms = (time.perf_counter() - started) * 1000.0

        log = logger.info if total_ms >= 250.0 else logger.debug
        log(
            "HUD build instance=%s node=%s generation_ready=%s topology_current=%s "
            "status_ms=%.1f semantic_check_ms=%.1f readiness_update_ms=%.1f "
            "hud_build_ms=%.1f coordinate_recheck_ms=%.1f snapshot_ms=%.1f total_ms=%.1f",
            instance_id,
            target_node_id,
            generation_ready,
            semantic_current,
            timings["status"],
            timings["semantic_check"],
            timings["readiness_update"],
            timings["hud_build"],
            timings["coordinate_recheck"],
            timings["snapshot"],
            total_ms,
        )
        return frame

    async def build_frame(
        self,
        instance_id: UUID,
        *,
        recent_limit: Optional[int] = None,
        token_budget: Optional[int] = None,
        wait_ms: int = 1200,
    ) -> Dict[str, Any]:
        return await self.prepare_frame(
            instance_id,
            recent_limit=recent_limit,
            token_budget=token_budget,
            wait_ms=wait_ms,
        )

    async def render_text_frame(
        self,
        instance_id: UUID,
        *,
        recent_limit: Optional[int] = None,
        token_budget: Optional[int] = None,
        wait_ms: int = 1200,
    ) -> str:
        frame = await self.build_frame(
            instance_id,
            recent_limit=recent_limit,
            token_budget=token_budget,
            wait_ms=wait_ms,
        )
        ready = await readiness_state(self.db, instance_id=instance_id)
        if (
            frame.get("hud", {}).get("generation_ready")
            and ready.get("hud_text")
            and ready.get("prepared_state_version") == frame.get("presence", {}).get("state_version")
        ):
            return ready["hud_text"]
        return render_hud_text(frame)

    async def apply_action(
        self,
        *,
        instance_id: UUID,
        expected_state_version: int,
        action_type: str,
        target_entity_id: Optional[UUID],
        text: Optional[str],
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(payload or {})
        state = await self.get_state(instance_id)
        if state["state_version"] != expected_state_version:
            raise RuntimeConflict(
                f"stale state_version {expected_state_version}; current is {state['state_version']}"
            )
        if state["lifecycle_state"] != "ready":
            raise RuntimeConflict(f"instance is not ready: {state['lifecycle_state']}")

        action_type = action_type.strip().lower()
        if action_type not in {"speak", "move", "inspect", "use_item", "wait", "custom"}:
            raise ValueError(f"unsupported action_type '{action_type}'")

        target = None
        if target_entity_id:
            target = await self.db.fetchrow(
                "SELECT entity_id, entity_type FROM aios.world_entity WHERE entity_id=$1 AND world_id=$2",
                target_entity_id,
                state["world_id"],
            )
            if not target:
                raise RuntimeNotFound("target entity is not present in this world")

        await self.causal.validate_runtime_action(
            world_id=state["world_id"],
            action_type=action_type,
            actor_entity_type=state["entity_type"],
            target_entity_type=target["entity_type"] if target else None,
            has_target=target is not None,
        )

        if action_type == "move" and target_entity_id is None:
            raise ValueError("move requires a location target")

        reserved = await self.db.execute_returning_row(
            """
            UPDATE aios.character_runtime_state
            SET lifecycle_state='acting', updated_at=now()
            WHERE instance_id=$1
              AND state_version=$2
              AND lifecycle_state='ready'
            RETURNING instance_id
            """,
            instance_id,
            expected_state_version,
        )
        if not reserved:
            raise RuntimeConflict("instance state changed before the action could be reserved")

        try:
            controller = await self.db.fetchrow(
                """
                SELECT controller_type, controller_ref
                FROM aios.entity_controller
                WHERE entity_id=$1 AND active=true
                ORDER BY CASE authority WHEN 'primary' THEN 0 ELSE 1 END, created_at
                LIMIT 1
                """,
                state["entity_id"],
            )
            controller_type = controller["controller_type"] if controller else "agent"
            controller_ref = controller["controller_ref"] if controller else None
            speaker_role = "user" if controller_type == "human" else "agent"
            viewpoint_id = (
                controller_ref
                if controller_type == "human" and controller_ref
                else state["character_id"]
            )

            event_payload = {
                **payload,
                "controller_type": controller_type,
                "controller_ref": controller_ref,
                "viewpoint_id": viewpoint_id,
                "runtime_instance_id": str(instance_id),
                "actor_entity_id": str(state["entity_id"]) if state["entity_id"] else None,
                "target_entity_id": str(target_entity_id) if target_entity_id else None,
                "action_type": action_type,
                "causal_intent": True,
            }
            message_text = text or f"[action:{action_type}]"
            event_kind = "chat_message" if action_type == "speak" and text else "other"
            ev = await self.db.execute_returning_row(
                """
                INSERT INTO aios.ingest_event (
                    event_time, source, kind, session_id, speaker_id, speaker_role,
                    recipient_id, viewpoint_id, character_id, user_name,
                    message_text, payload, dedupe_key
                )
                SELECT now(), 'world_runtime', $9::aios.event_kind, t.session_id,
                       $1, $8::aios.actor_type, $2, $7, ci.character_id, t.user_name,
                       $3, $4::jsonb, $5
                FROM aios.character_runtime_state rs
                JOIN aios.character_instance ci ON ci.instance_id=rs.instance_id
                JOIN aios.timeline t ON t.timeline_id=rs.timeline_id
                WHERE rs.instance_id=$6
                RETURNING event_id
                """,
                str(state["entity_id"]),
                str(target_entity_id) if target_entity_id else None,
                message_text,
                json.dumps(event_payload),
                f"runtime::{instance_id}::{expected_state_version}::{action_type}",
                instance_id,
                viewpoint_id,
                speaker_role,
                event_kind,
            )
            event_id = int(ev["event_id"])

            node_id, _ = await add_node_and_edge(
                self.db,
                timeline_id=state["timeline_id"],
                event_id=event_id,
                character_id=state["character_id"],
                kind=event_kind,
                speaker_id=str(state["entity_id"]),
                speaker_role=speaker_role,
                recipient_id=str(target_entity_id) if target_entity_id else None,
                message_text=message_text,
                payload=event_payload,
                viewpoint_id=viewpoint_id,
                edge_type="next",
            )

            causal_result = None
            if action_type == "move":
                causal_result = await self.causal.require_commit(
                    CausalCandidate(
                        world_id=state["world_id"],
                        timeline_id=state["timeline_id"],
                        dag_node_id=node_id,
                        domain_id="world.location",
                        event_type="move",
                        entity_id=state["entity_id"],
                        target_entity_id=target_entity_id,
                        state_key="location_entity_id",
                        value=str(target_entity_id),
                        source_kind="runtime",
                        source_ref=str(instance_id),
                        authority_kind="runtime_command",
                        parameters={
                            **payload,
                            "runtime_instance_id": str(instance_id),
                            "controller_type": controller_type,
                        },
                    )
                )

            updated = await self.db.execute_returning_row(
                """
                UPDATE aios.character_runtime_state
                SET head_node_id=$2,
                    lifecycle_state='ready',
                    state_version=state_version+1,
                    updated_at=now()
                WHERE instance_id=$1 AND state_version=$3
                RETURNING state_version
                """,
                instance_id,
                node_id,
                expected_state_version,
            )
            if not updated:
                raise RuntimeConflict("state changed while action was being committed")

            return {
                "ok": True,
                "world_event_id": (
                    causal_result.get("world_event_id") if causal_result else None
                ),
                "causal": causal_result,
                "event_id": event_id,
                "node_id": node_id,
                "state_version": updated["state_version"],
            }
        except CausalRejected as exc:
            await self.db.execute(
                """
                UPDATE aios.character_runtime_state
                SET lifecycle_state='ready', updated_at=now()
                WHERE instance_id=$1
                """,
                instance_id,
            )
            raise ValueError(str(exc)) from exc
        except Exception:
            await self.db.execute(
                """
                UPDATE aios.character_runtime_state
                SET lifecycle_state='ready', updated_at=now()
                WHERE instance_id=$1 AND lifecycle_state='acting'
                """,
                instance_id,
            )
            raise

    async def fork_instance(
        self,
        *,
        source_instance_id: UUID,
        target_world_id: Optional[UUID] = None,
        target_world_key: Optional[str] = None,
    ) -> ActivationResult:
        source = await self.get_state(source_instance_id)
        target = await self._resolve_world(
            world_id=target_world_id,
            world_key=target_world_key,
            home_world_id=None,
        )
        timeline = await self.db.fetchrow(
            """
            SELECT session_id, user_name, scope_key
            FROM aios.timeline
            WHERE timeline_id=$1
            """,
            source["timeline_id"],
        )
        if not timeline:
            raise RuntimeNotFound("source runtime timeline is missing")

        new_instance = await self.db.execute_returning_row(
            """
            INSERT INTO aios.character_instance (
                character_id, world_id, current_world_id, parent_instance_id,
                forked_from_node_id, meta
            )
            VALUES (
                $1,$2,$2,$3,$4,
                jsonb_build_object(
                    'runtime_user_name',$5,
                    'forked_from_instance_id',$3::text
                )
            )
            RETURNING instance_id
            """,
            source["character_id"],
            target["world_id"],
            source_instance_id,
            source["head_node_id"],
            timeline["user_name"],
        )
        instance_id = new_instance["instance_id"]

        timeline_id = await get_or_create_timeline(
            self.db,
            world_key=target["world_key"],
            session_id=timeline["session_id"],
            character_id=source["character_id"],
            user_name=timeline["user_name"],
            scope_key=f"{timeline['scope_key']}:fork:{instance_id}",
            meta={
                "runtime_instance_id": str(instance_id),
                "forked_from_instance_id": str(source_instance_id),
                "forked_from_node_id": str(source["head_node_id"]) if source["head_node_id"] else None,
                "world_runtime": True,
            },
        )

        entity = await self.db.execute_returning_row(
            """
            INSERT INTO aios.world_entity (
                world_id, entity_key, entity_type, display_name,
                character_instance_id, meta
            )
            SELECT $1,$2,'character',COALESCE(ci.display_name, ci.character_id),
                   $3,jsonb_build_object('forked_from_entity_id',$4::text)
            FROM aios.character_identity ci
            WHERE ci.character_id=$5
            RETURNING entity_id
            """,
            target["world_id"],
            f"character-instance:{instance_id}",
            instance_id,
            source["entity_id"],
            source["character_id"],
        )

        await self.db.execute(
            """
            INSERT INTO aios.entity_controller (
                entity_id, controller_type, controller_ref, authority, active, meta
            )
            SELECT $1, controller_type, controller_ref, authority, active,
                   meta || jsonb_build_object('copied_on_fork',true)
            FROM aios.entity_controller
            WHERE entity_id=$2 AND active=true
            ON CONFLICT (entity_id, controller_type, controller_ref) DO NOTHING
            """,
            entity["entity_id"],
            source["entity_id"],
        )

        # Only /char-owned runtime state is copied here. Objective physical
        # state is not copied through character_runtime_state; causal branch
        # state must be explicitly mapped into the target world.
        await self.db.execute(
            """
            INSERT INTO aios.character_runtime_state (
                instance_id, world_id, timeline_id, head_node_id,
                source_timeline_id, source_head_node_id,
                lifecycle_state, emotional_state, social_state,
                goals, active_tasks, runtime_flags, state_version
            )
            SELECT
                $1,$2,$3,NULL,
                source_timeline_id, source_head_node_id,
                'ready',emotional_state,social_state,
                goals,active_tasks,
                runtime_flags || jsonb_build_object(
                    'forked_from_instance_id',$4::text,
                    'forked_from_node_id',$5::text
                ),
                1
            FROM aios.character_runtime_state
            WHERE instance_id=$4
            """,
            instance_id,
            target["world_id"],
            timeline_id,
            source_instance_id,
            source["head_node_id"],
        )

        # Subjective knowledge remains a /char fork operation and intentionally
        # stays separate from deterministic branch state.
        await self.db.execute(
            """
            INSERT INTO aios.character_proposition_knowledge (
                instance_id, proposition_id, epistemic_status, confidence,
                acquisition_mode, source_entity_id, first_node_id, last_node_id,
                first_acquired_at, updated_at, meta,
                base_confidence, attention_weight, trust_weight,
                compatibility_weight, retention_weight, salience_weight,
                effective_confidence
            )
            SELECT $1, proposition_id, epistemic_status, confidence,
                   acquisition_mode, NULL, first_node_id, last_node_id,
                   first_acquired_at, now(),
                   meta || jsonb_build_object('copied_on_fork',true),
                   base_confidence, attention_weight, trust_weight,
                   compatibility_weight, retention_weight, salience_weight,
                   effective_confidence
            FROM aios.character_proposition_knowledge
            WHERE instance_id=$2
            ON CONFLICT (instance_id, proposition_id) DO NOTHING
            """,
            instance_id,
            source_instance_id,
        )

        await self.db.execute(
            """
            INSERT INTO aios.character_knowledge (
                instance_id, claim_id, epistemic_status, confidence,
                source_entity_id, first_node_id, last_node_id, meta
            )
            SELECT $1, claim_id, epistemic_status, confidence,
                   NULL, first_node_id, last_node_id,
                   meta || jsonb_build_object('copied_on_fork',true)
            FROM aios.character_knowledge
            WHERE instance_id=$2
            ON CONFLICT (instance_id, claim_id) DO NOTHING
            """,
            instance_id,
            source_instance_id,
        )

        await self._seed_fork_location(
            source=source,
            target_world_id=target["world_id"],
            target_timeline_id=timeline_id,
            target_entity_id=entity["entity_id"],
        )

        return await self._activation_result(instance_id)

    async def _seed_fork_location(
        self,
        *,
        source: Dict[str, Any],
        target_world_id: UUID,
        target_timeline_id: UUID,
        target_entity_id: UUID,
    ) -> None:
        source_location = await self.causal.state(
            world_id=source["world_id"],
            timeline_id=source["timeline_id"],
            domain_id="world.location",
            entity_id=source["entity_id"],
            state_key="location_entity_id",
        )
        if not source_location or source_location.value is None:
            return

        source_location_entity = await self.db.fetchrow(
            """
            SELECT entity_key
            FROM aios.world_entity
            WHERE world_id=$1 AND entity_id=$2
            """,
            source["world_id"],
            UUID(str(source_location.value)),
        )
        if not source_location_entity or not source_location_entity["entity_key"]:
            return
        target_location = await self.db.fetchrow(
            """
            SELECT entity_id
            FROM aios.world_entity
            WHERE world_id=$1 AND entity_key=$2
            """,
            target_world_id,
            source_location_entity["entity_key"],
        )
        if not target_location:
            return

        await self.causal.require_commit(
            CausalCandidate(
                world_id=target_world_id,
                timeline_id=target_timeline_id,
                domain_id="world.location",
                event_type="set_location",
                entity_id=target_entity_id,
                target_entity_id=target_location["entity_id"],
                state_key="location_entity_id",
                value=str(target_location["entity_id"]),
                source_kind="branch_fork",
                source_ref=str(source["instance_id"]),
                authority_kind="branch_snapshot",
                parameters={"forked_from_timeline_id": str(source["timeline_id"])},
            )
        )

    async def create_entity(
        self,
        *,
        world_id: UUID,
        entity_key: Optional[str],
        entity_type: str,
        display_name: Optional[str],
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        world = await self.db.fetchrow(
            "SELECT world_id FROM aios.world WHERE world_id=$1",
            world_id,
        )
        if not world:
            raise RuntimeNotFound(f"Unknown world {world_id}")
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.world_entity (
                world_id, entity_key, entity_type, display_name, meta
            )
            VALUES ($1,$2,$3,$4,$5::jsonb)
            RETURNING entity_id, world_id, entity_key, entity_type, display_name, meta
            """,
            world_id,
            entity_key,
            entity_type,
            display_name,
            json.dumps(meta or {}),
        )
        return dict(row)

    async def relate_entities(
        self,
        *,
        world_id: UUID,
        subject_entity_id: UUID,
        relation_type: str,
        object_entity_id: UUID,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        rows = await self.db.fetch(
            """
            SELECT entity_id
            FROM aios.world_entity
            WHERE world_id=$1 AND entity_id = ANY($2::uuid[])
            """,
            world_id,
            [subject_entity_id, object_entity_id],
        )
        if len(rows) != 2:
            raise RuntimeNotFound("both relation endpoints must exist in the same world")
        if relation_type == "located_in":
            raise ValueError(
                "located_in is causal state; use a move/deterministic location event instead"
            )
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.world_entity_relation (
                world_id, subject_entity_id, relation_type, object_entity_id, meta
            )
            VALUES ($1,$2,$3,$4,$5::jsonb)
            RETURNING relation_id, world_id, subject_entity_id,
                      relation_type, object_entity_id, meta
            """,
            world_id,
            subject_entity_id,
            relation_type,
            object_entity_id,
            json.dumps(meta or {}),
        )
        return dict(row)

    async def upsert_rule(
        self,
        *,
        world_id: UUID,
        rule_key: str,
        rule_type: str,
        enabled: bool,
        priority: int,
        rule_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.world_rule (
                world_id, rule_key, rule_type, enabled, priority, rule_data
            )
            VALUES ($1,$2,$3,$4,$5,$6::jsonb)
            ON CONFLICT (world_id, rule_key) DO UPDATE
            SET rule_type=EXCLUDED.rule_type,
                enabled=EXCLUDED.enabled,
                priority=EXCLUDED.priority,
                rule_data=EXCLUDED.rule_data,
                updated_at=now()
            RETURNING rule_id, world_id, rule_key, rule_type,
                      enabled, priority, rule_data
            """,
            world_id,
            rule_key,
            rule_type,
            enabled,
            priority,
            json.dumps(rule_data),
        )
        if not row:
            raise RuntimeNotFound(f"Unknown world {world_id}")
        return dict(row)

    async def add_controller(
        self,
        *,
        entity_id: UUID,
        controller_type: str,
        controller_ref: str,
        authority: str = "primary",
    ) -> Dict[str, Any]:
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.entity_controller (
                entity_id, controller_type, controller_ref, authority
            )
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (entity_id, controller_type, controller_ref)
            DO UPDATE SET authority=EXCLUDED.authority, active=true
            RETURNING controller_id, entity_id, controller_type,
                      controller_ref, authority, active
            """,
            entity_id,
            controller_type,
            controller_ref,
            authority,
        )
        return dict(row)

    async def _resolve_world(
        self,
        *,
        world_id: Optional[UUID],
        world_key: Optional[str],
        home_world_id: Optional[UUID],
    ):
        if world_id:
            row = await self.db.fetchrow(
                "SELECT world_id, world_key FROM aios.world WHERE world_id=$1",
                world_id,
            )
        elif world_key:
            row = await self.db.fetchrow(
                "SELECT world_id, world_key FROM aios.world WHERE world_key=$1",
                world_key,
            )
        elif home_world_id:
            row = await self.db.fetchrow(
                "SELECT world_id, world_key FROM aios.world WHERE world_id=$1",
                home_world_id,
            )
        else:
            row = await self.db.fetchrow(
                """
                SELECT world_id, world_key
                FROM aios.world
                WHERE world_type IN ('asserted','runtime','canonical')
                ORDER BY created_at
                LIMIT 1
                """
            )
        if not row:
            raise RuntimeNotFound(
                "No concrete world resolved. Supply world_id/world_key or set character home_world_id."
            )
        if not row["world_key"]:
            raise RuntimeNotFound("Runtime worlds require a stable world_key")
        return row

    async def _activation_result(self, instance_id: UUID) -> ActivationResult:
        row = await self.db.fetchrow(
            """
            SELECT ci.character_id, ci.instance_id, we.entity_id,
                   rs.world_id, rs.timeline_id, rs.head_node_id,
                   rs.state_version, rs.lifecycle_state
            FROM aios.character_instance ci
            JOIN aios.character_runtime_state rs ON rs.instance_id=ci.instance_id
            JOIN aios.world_entity we ON we.character_instance_id=ci.instance_id
            WHERE ci.instance_id=$1
            """,
            instance_id,
        )
        return ActivationResult(**dict(row))
