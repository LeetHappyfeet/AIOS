from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

from aios_app.db import Database
from aios_app.dag import get_or_create_timeline, add_node_and_edge
from aios_app.hud.frame import HUDAssembler
from aios_app.hud.render_text import render_hud_text
from aios_app.world.topology import (
    ensure_character_root_world,
    ensure_runtime_branch_world,
    latest_source_anchor,
)


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
    """SQL-backed authoritative runtime state for one shared AIOS world."""

    def __init__(self, db: Database):
        self.db = db
        self.hud = HUDAssembler(db)

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
        return await self._activation_result(instance_id)

    async def get_state(self, instance_id: UUID) -> Dict[str, Any]:
        row = await self.db.fetchrow(
            """
            SELECT
                rs.*, ci.character_id, we.entity_id, we.entity_type,
                we.display_name, w.world_key
            FROM aios.character_runtime_state rs
            JOIN aios.character_instance ci ON ci.instance_id=rs.instance_id
            JOIN aios.world w ON w.world_id=rs.world_id
            LEFT JOIN aios.world_entity we ON we.character_instance_id=rs.instance_id
            WHERE rs.instance_id=$1
            """,
            instance_id,
        )
        if not row:
            raise RuntimeNotFound(f"Unknown runtime instance {instance_id}")
        return dict(row)

    async def build_frame(
        self,
        instance_id: UUID,
        *,
        recent_limit: Optional[int] = None,
        token_budget: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Coordinate construction of the canonical branch-aware RPG HUD."""
        try:
            return await self.hud.build(
                instance_id,
                recent_limit=recent_limit,
                token_budget=token_budget,
            )
        except LookupError as exc:
            raise RuntimeNotFound(str(exc)) from exc

    async def render_text_frame(
        self,
        instance_id: UUID,
        *,
        recent_limit: Optional[int] = None,
        token_budget: Optional[int] = None,
    ) -> str:
        """Render exactly the same canonical HUD returned by build_frame()."""
        frame = await self.build_frame(
            instance_id,
            recent_limit=recent_limit,
            token_budget=token_budget,
        )
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

        await self._validate_action_rules(
            world_id=state["world_id"],
            action_type=action_type,
            actor_entity_type=state["entity_type"],
            target_entity_type=target["entity_type"] if target else None,
            has_target=target is not None,
        )

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
        speaker_role = "user" if controller_type == "human" else "agent"

        event_payload = {
            **payload,
            "controller_type": controller_type,
            "controller_ref": controller["controller_ref"] if controller else None,
            "runtime_instance_id": str(instance_id),
            "actor_entity_id": str(state["entity_id"]) if state["entity_id"] else None,
            "target_entity_id": str(target_entity_id) if target_entity_id else None,
            "action_type": action_type,
        }
        message_text = text or f"[action:{action_type}]"
        event_kind = "chat_message" if action_type == "speak" and text else "other"
        ev = await self.db.execute_returning_row(
            """
            INSERT INTO aios.ingest_event (
                event_time, source, kind, session_id, speaker_id, speaker_role,
                recipient_id, character_id, user_name, message_text, payload,
                dedupe_key
            )
            SELECT now(), 'world_runtime', $8::aios.event_kind, t.session_id,
                   $1, $7::aios.actor_type, $2, ci.character_id, t.user_name, $3,
                   $4::jsonb, $5
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
            edge_type="next",
        )

        if action_type == "move" and target_entity_id:
            await self.db.execute(
                "UPDATE aios.character_runtime_state SET location_entity_id=$2 WHERE instance_id=$1",
                instance_id,
                target_entity_id,
            )

        world_event = await self.db.execute_returning_row(
            """
            INSERT INTO aios.world_event (
                world_id, timeline_id, instance_id, actor_entity_id,
                target_entity_id, action_type, payload, dag_node_id
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
            RETURNING world_event_id
            """,
            state["world_id"],
            state["timeline_id"],
            instance_id,
            state["entity_id"],
            target_entity_id,
            action_type,
            json.dumps(event_payload),
            node_id,
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
            "world_event_id": world_event["world_event_id"],
            "event_id": event_id,
            "node_id": node_id,
            "state_version": updated["state_version"],
        }

    async def fork_instance(
        self,
        *,
        source_instance_id: UUID,
        target_world_id: Optional[UUID] = None,
        target_world_key: Optional[str] = None,
    ) -> ActivationResult:
        """Fork one experiential continuation into another concrete world."""
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

        await self.db.execute(
            """
            INSERT INTO aios.character_runtime_state (
                instance_id, world_id, timeline_id, head_node_id,
                source_timeline_id, source_head_node_id,
                lifecycle_state, health, stamina, energy,
                physical_state, emotional_state, social_state,
                goals, active_tasks, runtime_flags, state_version
            )
            SELECT
                $1,$2,$3,NULL,
                source_timeline_id, source_head_node_id,
                'ready',health,stamina,energy,
                physical_state,emotional_state,social_state,
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

        return await self._activation_result(instance_id)

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

    async def _validate_action_rules(
        self,
        *,
        world_id: UUID,
        action_type: str,
        actor_entity_type: Optional[str],
        target_entity_type: Optional[str],
        has_target: bool,
    ) -> None:
        """Apply deterministic world-level action constraints before mutation."""
        rules = await self.db.fetch(
            """
            SELECT rule_key, rule_type, rule_data
            FROM aios.world_rule
            WHERE world_id=$1 AND enabled=true
            ORDER BY priority, rule_key
            """,
            world_id,
        )

        for rule in rules:
            data = dict(rule["rule_data"] or {})
            rule_type = rule["rule_type"]

            if rule_type == "action_allowlist":
                actions = set(data.get("actions") or [])
                if actions and action_type not in actions:
                    raise ValueError(
                        f"world rule '{rule['rule_key']}' does not allow action '{action_type}'"
                    )

            elif rule_type == "require_target":
                applies = data.get("action_type", "*")
                if applies in ("*", action_type) and not has_target:
                    raise ValueError(
                        f"world rule '{rule['rule_key']}' requires a target for '{action_type}'"
                    )

            elif rule_type == "require_target_type":
                applies = data.get("action_type", "*")
                allowed_types = set(data.get("target_entity_types") or [])
                if applies in ("*", action_type) and allowed_types:
                    if target_entity_type not in allowed_types:
                        raise ValueError(
                            f"world rule '{rule['rule_key']}' rejects target type "
                            f"'{target_entity_type}' for '{action_type}'"
                        )

            elif rule_type == "deny_action":
                applies = data.get("action_type", "*")
                if applies not in ("*", action_type):
                    continue
                actor_filter = data.get("actor_entity_type")
                target_filter = data.get("target_entity_type")
                if actor_filter and actor_filter != actor_entity_type:
                    continue
                if target_filter and target_filter != target_entity_type:
                    continue
                raise ValueError(
                    f"world rule '{rule['rule_key']}' denies action '{action_type}'"
                )

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
