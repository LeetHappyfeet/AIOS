from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.cognitive_context import CognitiveContextService
from aios_app.epistemic.weights import get_profile
from aios_app.hud.context import HUDContext, HUDContextResolver
from aios_app.hud.profile import get_profile as get_hud_profile
from aios_app.hud.relevance import HUDRelevanceScorer
from aios_app.plugins.manager import PluginManager
from aios_app.plugins.types import PluginRuntimeContext


@dataclass(frozen=True)
class HUDBudget:
    """Approximate token caps. These are selection budgets, not tokenizer guarantees."""

    total_tokens: int = 1600
    section_tokens: Mapping[str, int] = field(
        default_factory=lambda: {
            "scene": 260,
            "relationships": 160,
            "inventory": 140,
            "memories": 350,
            "beliefs": 320,
            "goals": 140,
            "rules": 140,
            "recent_events": 220,
        }
    )


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _approx_tokens(value: Any) -> int:
    return max(1, (len(str(value)) + 3) // 4)


def _trim_to_budget(
    items: Iterable[dict[str, Any]],
    token_cap: int,
    text_fn: Callable[[dict[str, Any]], str],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for item in items:
        cost = _approx_tokens(text_fn(item))
        if selected and used + cost > token_cap:
            continue
        if not selected and cost > token_cap:
            clipped = dict(item)
            text = text_fn(item)
            clipped["text"] = text[: max(64, token_cap * 4)]
            selected.append(clipped)
            break
        selected.append(item)
        used += cost
    return selected


class HUDAssembler:
    """Build the canonical prompt-ready HUD from already-resolved cognition.

    Historical/source eligibility, semantic retrieval, flat-knowledge fallback,
    deduplication, and semantic admission live in CognitiveContextService.
    This layer performs attention scoring, presentation shaping, token budgeting,
    and deterministic frame assembly only.
    """

    def __init__(
        self,
        db: Database,
        *,
        budget: Optional[HUDBudget] = None,
        plugin_manager: Optional[PluginManager] = None,
    ):
        self.db = db
        self.context_resolver = HUDContextResolver(db)
        self.cognition = CognitiveContextService(db)
        self.budget = budget or HUDBudget()
        self.plugin_manager = plugin_manager or PluginManager()

    async def build(
        self,
        instance_id: UUID,
        *,
        recent_limit: Optional[int] = None,
        token_budget: Optional[int] = None,
    ) -> dict[str, Any]:
        context = await self.context_resolver.resolve(instance_id)
        raw_state = await self._runtime_state(instance_id)
        plugin_snapshot = await self.plugin_manager.collect(
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
        hud_profile = await get_hud_profile(self.db, character_id=context.character_id)
        effective_recent_limit = (
            hud_profile.recent_event_limit if recent_limit is None else recent_limit
        )

        attention = await self.cognition.resolve_attention_inputs(
            context,
            raw_state,
            plugin_snapshot,
            recent_limit=effective_recent_limit,
        )
        scorer = HUDRelevanceScorer(
            context,
            focus_text=attention.retrieval_focus_text,
            goals=attention.goals,
        )
        cognitive_snapshot = await self.cognition.resolve_knowledge(
            context,
            scorer,
            attention,
            hud_profile,
        )
        knowledge = list(cognitive_snapshot.knowledge)

        section_caps = {
            "scene": hud_profile.scene_budget,
            "relationships": hud_profile.relationship_budget,
            "inventory": hud_profile.inventory_budget,
            "memories": hud_profile.memory_budget,
            "beliefs": hud_profile.belief_budget,
            "goals": hud_profile.goals_budget,
            "rules": hud_profile.rules_budget,
            "recent_events": max(32, int(hud_profile.token_budget * 0.14)),
        }
        resolved_total = hud_profile.token_budget
        if token_budget is not None and token_budget > 0:
            scale = token_budget / max(1, hud_profile.token_budget)
            section_caps = {
                key: max(32, int(value * scale))
                for key, value in section_caps.items()
            }
            resolved_total = token_budget

        identity = await self._identity(context)
        scene = await self._scene(
            context,
            scorer,
            token_cap=section_caps["scene"],
            entity_hops=hud_profile.entity_hops,
        )
        relationships = (
            await self._relationships(context, scorer)
            if hud_profile.include_relationships
            else []
        )
        inventory = (
            await self._inventory(context, scorer)
            if hud_profile.include_inventory
            else []
        )

        if not hud_profile.include_conflicts:
            for item in knowledge:
                item["conflicts"] = []
        if not hud_profile.include_provenance:
            for item in knowledge:
                for key in (
                    "source_entity_id",
                    "source_world_id",
                    "source_node_id",
                    "acquisition_mode",
                    "predicate_family",
                    "topology",
                ):
                    item.pop(key, None)
                anchor = item.get("anchor")
                if anchor:
                    item["anchor"] = {
                        "relationship": anchor.get("relationship"),
                        "target_type": anchor.get("target_type"),
                        "target_label": anchor.get("target_label"),
                        "world_visible": bool(anchor.get("world_visible")),
                    }
                if item.get("world_context"):
                    item["world_context"] = [
                        {
                            "node_type": entry.get("node_type"),
                            "label": entry.get("label"),
                            "edge_type": entry.get("edge_type"),
                        }
                        for entry in item["world_context"]
                    ]
        if not hud_profile.include_confidence:
            for item in knowledge:
                for key in (
                    "confidence",
                    "base_confidence",
                    "effective_confidence",
                    "attention_weight",
                    "trust_weight",
                    "compatibility_weight",
                    "retention_weight",
                    "salience_weight",
                ):
                    item.pop(key, None)

        rules = await self._rules(context, scorer)
        recent_events = self._recent_events(attention.recent_newest, scorer)

        memories: list[dict[str, Any]] = []
        beliefs: list[dict[str, Any]] = []
        semantic_goals: list[dict[str, Any]] = []
        semantic_rules: list[dict[str, Any]] = []
        semantic_events: list[dict[str, Any]] = []
        for item in knowledge:
            kind = str(item.get("claim_kind") or "BELIEF").upper()
            if kind == "MEMORY":
                memories.append(item)
            elif kind == "GOAL":
                semantic_goals.append(item)
            elif kind == "RULE":
                semantic_rules.append(item)
            elif kind == "EVENT":
                semantic_events.append(item)
            else:
                beliefs.append(item)

        goal_items = [
            {"text": str(goal), "source": "runtime", "tier": 0}
            for goal in attention.goals
        ] + semantic_goals
        rule_items = rules + [
            {**item, "source": "character_knowledge"}
            for item in semantic_rules
        ]
        event_items = list(reversed(recent_events)) + semantic_events

        memories = _trim_to_budget(
            memories, section_caps["memories"], lambda x: x.get("text", "")
        )
        beliefs = _trim_to_budget(
            beliefs, section_caps["beliefs"], lambda x: x.get("text", "")
        )
        relationships = _trim_to_budget(
            relationships,
            section_caps["relationships"],
            lambda x: f"{x.get('display_name','')} {x.get('relationship_type','')} {x.get('meta','')}",
        )
        inventory = _trim_to_budget(
            inventory,
            section_caps["inventory"],
            lambda x: f"{x.get('display_name','')} {x.get('state','')}",
        )
        goal_items = _trim_to_budget(
            goal_items, section_caps["goals"], lambda x: x.get("text", "")
        )
        rule_items = _trim_to_budget(
            rule_items,
            section_caps["rules"],
            lambda x: x.get("text") or x.get("rule_key") or "",
        )
        event_items = _trim_to_budget(
            event_items,
            section_caps["recent_events"],
            lambda x: x.get("message_text") or x.get("text") or "",
        )

        suppressed = cognitive_snapshot.firewall_suppressed
        return {
            "identity": identity,
            "presence": {
                "instance_id": context.instance_id,
                "entity_id": context.entity_id,
                "world_id": context.world_id,
                "world_key": context.world_key,
                "timeline_id": context.timeline_id,
                "head_node_id": context.head_node_id,
                "source_timeline_id": context.source_timeline_id,
                "source_head_node_id": context.source_head_node_id,
                "state_version": context.state_version,
                "lifecycle_state": context.lifecycle_state,
                "location_entity_id": context.location_entity_id,
            },
            "scene": scene,
            "state": {
                "health": raw_state.get("health"),
                "stamina": raw_state.get("stamina"),
                "energy": raw_state.get("energy"),
                "physical": (
                    _json_value(raw_state.get("physical_state"), {})
                    if hud_profile.include_physical_state
                    else {}
                ),
                "emotional": (
                    _json_value(raw_state.get("emotional_state"), {})
                    if hud_profile.include_emotional_state
                    else {}
                ),
                "social": (
                    _json_value(raw_state.get("social_state"), {})
                    if hud_profile.include_social_state
                    else {}
                ),
                "active_tasks": _json_value(raw_state.get("active_tasks"), []),
                "flags": _json_value(raw_state.get("runtime_flags"), {}),
            },
            "relationships": relationships,
            "inventory": inventory,
            "memories": memories,
            "beliefs": beliefs,
            "goals": goal_items,
            "rules": rule_items,
            "recent_events": event_items,
            "plugins": plugin_snapshot.get("plugins") or {},
            "plugin_sections": plugin_snapshot.get("sections") or [],
            "actions": [
                "speak",
                "move",
                "inspect",
                "use_item",
                "wait",
                "custom",
                *[
                    str(action.get("key"))
                    for action in (plugin_snapshot.get("actions") or [])
                    if action.get("key")
                ],
            ],
            "hud": {
                "version": "hud-v1",
                "profile_id": hud_profile.profile_id,
                "profile_name": hud_profile.profile_name,
                "selection": "pre-resolved-cognition/entity-centered/deterministic",
                "token_budget": resolved_total,
                "section_token_budgets": section_caps,
                "world_lineage": list(context.lineage_world_ids),
                "instance_lineage": list(context.lineage_instance_ids),
                "topology_retrieval": cognitive_snapshot.topology_retrieval,
                "topology_partial_fallback": cognitive_snapshot.topology_partial_fallback,
                "anchor_retrieval": cognitive_snapshot.anchored_knowledge_count > 0,
                "anchor_partial_fallback": (
                    bool(knowledge)
                    and cognitive_snapshot.anchored_knowledge_count < len(knowledge)
                ),
                "anchor_count": cognitive_snapshot.anchored_knowledge_count,
                "anchor_invisible_count": cognitive_snapshot.invisible_anchor_count,
                "world_context_count": cognitive_snapshot.visible_world_context_count,
                "source_cursor_bounded": bool(
                    context.source_timeline_id and context.source_head_node_id
                ),
                "cognitive_firewall": {
                    "admitted": len(knowledge),
                    "suppressed": sum(suppressed.values()),
                    "suppressed_by_reason": suppressed,
                    "stage": "pre_hud",
                },
                "focus_text": attention.focus_text,
                "plugin_focus_text": attention.plugin_focus_text,
                "plugin_status": plugin_snapshot.get("status") or {},
                "plugin_retrieval_signals": plugin_snapshot.get("retrieval_signals") or [],
            },
        }

    async def _runtime_state(self, instance_id: UUID) -> dict[str, Any]:
        row = await self.db.fetchrow(
            "SELECT * FROM aios.character_runtime_state WHERE instance_id=$1",
            instance_id,
        )
        if not row:
            raise LookupError(f"Unknown runtime instance {instance_id}")
        return dict(row)

    async def _identity(self, context: HUDContext) -> dict[str, Any]:
        row = await self.db.fetchrow(
            """
            SELECT character_id, display_name, canonical_name, species, gender,
                   visual_summary, primary_role, archetype, default_tone,
                   speech_style, moral_constraints, meta
            FROM aios.character_identity
            WHERE character_id=$1
            """,
            context.character_id,
        )
        identity = dict(row) if row else {"character_id": context.character_id}
        identity["epistemic_profile"] = await get_profile(
            self.db, character_id=context.character_id
        )
        return identity

    async def _scene(
        self,
        context: HUDContext,
        scorer: HUDRelevanceScorer,
        *,
        token_cap: int,
        entity_hops: int,
    ) -> dict[str, Any]:
        seed_ids = list(context.scene_entity_ids)
        rows = await self.db.fetch(
            """
            WITH RECURSIVE expanded(entity_id, depth) AS (
                SELECT unnest($3::uuid[]), 0
                UNION
                SELECT
                    CASE
                        WHEN r.subject_entity_id=x.entity_id THEN r.object_entity_id
                        ELSE r.subject_entity_id
                    END,
                    x.depth + 1
                FROM expanded x
                JOIN aios.world_entity_relation r
                  ON r.world_id=$1
                 AND r.valid_to_node_id IS NULL
                 AND (
                     r.subject_entity_id=x.entity_id
                     OR r.object_entity_id=x.entity_id
                 )
                WHERE x.depth < $4
            )
            SELECT DISTINCT
                e.entity_id, e.entity_type, e.display_name, e.entity_key, e.meta,
                e.created_at,
                MIN(x.depth) OVER (PARTITION BY e.entity_id) AS graph_depth
            FROM aios.world_entity e
            JOIN expanded x ON x.entity_id=e.entity_id
            WHERE e.world_id=$1
              AND e.entity_id IS DISTINCT FROM $2
            ORDER BY graph_depth, e.created_at
            LIMIT 200
            """,
            context.world_id,
            context.entity_id,
            seed_ids,
            max(0, min(int(entity_hops), 4)),
        )
        entities: list[dict[str, Any]] = []
        location = None
        for rank, row in enumerate(rows):
            item = dict(row)
            item["tier"] = (
                0
                if context.entity_is_active(item["entity_id"])
                else min(2, int(item.get("graph_depth") or 0) + 1)
            )
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=f"{item.get('display_name','')} {item.get('entity_key','')} {item.get('meta','')}",
                candidate_world_id=context.world_id,
                candidate_entity_id=item["entity_id"],
            )
            item["relevance"] = score.as_dict()
            entities.append(item)
            if item["entity_id"] == context.location_entity_id:
                location = item

        entities.sort(
            key=lambda item: (item.get("tier", 9), -item["relevance"]["total"])
        )
        entities = _trim_to_budget(
            entities,
            token_cap,
            lambda x: f"{x.get('display_name','')} {x.get('entity_type','')} {x.get('meta','')}",
        )
        selected_ids = [context.entity_id] + [item["entity_id"] for item in entities]
        relations = await self.db.fetch(
            """
            SELECT relation_type, subject_entity_id, object_entity_id, meta
            FROM aios.world_entity_relation
            WHERE world_id=$1
              AND valid_to_node_id IS NULL
              AND subject_entity_id = ANY($2::uuid[])
              AND object_entity_id = ANY($2::uuid[])
            """,
            context.world_id,
            selected_ids,
        )
        actors = [
            item
            for item in entities
            if str(item.get("entity_type", "")).lower()
            in {"character", "person", "agent", "user"}
        ]
        objects = [item for item in entities if item not in actors and item is not location]
        return {
            "location": location,
            "actors": actors,
            "objects": objects,
            "relations": [dict(row) for row in relations],
        }

    async def _relationships(
        self, context: HUDContext, scorer: HUDRelevanceScorer
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT
                cr.target_entity_id AS entity_id,
                cr.relationship_type,
                cr.affinity,
                cr.trust,
                cr.familiarity,
                cr.meta,
                cr.updated_at,
                e.entity_type,
                e.display_name,
                e.entity_key
            FROM aios.character_relationship cr
            JOIN aios.world_entity e ON e.entity_id=cr.target_entity_id
            WHERE cr.observer_instance_id=$1
              AND e.world_id=$2
            ORDER BY cr.updated_at DESC
            """,
            context.instance_id,
            context.world_id,
        )
        result = []
        for rank, row in enumerate(rows):
            item = dict(row)
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=f"{item.get('display_name','')} {item.get('relationship_type','')} {item.get('meta','')}",
                candidate_world_id=context.world_id,
                candidate_entity_id=item["entity_id"],
                updated_at=item.get("updated_at"),
            )
            item["tier"] = 0 if context.entity_is_active(item["entity_id"]) else 2
            item["relevance"] = score.as_dict()
            result.append(item)
        result.sort(key=lambda x: (x["tier"], -x["relevance"]["total"]))
        return result

    async def _inventory(
        self, context: HUDContext, scorer: HUDRelevanceScorer
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT i.quantity, i.equipped, i.state, i.updated_at,
                   e.entity_id, e.entity_type, e.display_name, e.entity_key, e.meta
            FROM aios.character_inventory i
            JOIN aios.world_entity e ON e.entity_id=i.entity_id
            WHERE i.instance_id=$1
            ORDER BY i.equipped DESC, i.updated_at DESC
            """,
            context.instance_id,
        )
        result = []
        for rank, row in enumerate(rows):
            item = dict(row)
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=f"{item.get('display_name','')} {item.get('entity_key','')} {item.get('state','')}",
                candidate_world_id=context.world_id,
                candidate_entity_id=item["entity_id"],
                updated_at=item.get("updated_at"),
            )
            item["tier"] = 0 if item.get("equipped") else 1
            item["relevance"] = score.as_dict()
            result.append(item)
        result.sort(key=lambda x: (x["tier"], -x["relevance"]["total"]))
        return result

    async def _rules(
        self, context: HUDContext, scorer: HUDRelevanceScorer
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT world_id, rule_key, rule_type, priority, rule_data, updated_at
            FROM aios.world_rule
            WHERE world_id = ANY($1::uuid[])
              AND enabled=true
            ORDER BY (world_id=$2) DESC, priority, rule_key
            """,
            list(context.lineage_world_ids),
            context.world_id,
        )
        result = []
        for rank, row in enumerate(rows):
            item = dict(row)
            rule_data = _json_value(item.get("rule_data"), {})
            item["text"] = (
                rule_data.get("text")
                or rule_data.get("description")
                or item.get("rule_key")
            )
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=f"{item.get('rule_key','')} {item.get('rule_type','')} {rule_data}",
                candidate_world_id=item.get("world_id"),
                updated_at=item.get("updated_at"),
            )
            if score.branch_penalty:
                continue
            item["tier"] = 0 if item.get("world_id") == context.world_id else 1
            item["relevance"] = score.as_dict()
            result.append(item)
        result.sort(
            key=lambda x: (
                x["tier"],
                x.get("priority", 100),
                -x["relevance"]["total"],
            )
        )
        return result

    def _recent_events(
        self,
        rows_newest_first: list[dict[str, Any]],
        scorer: HUDRelevanceScorer,
    ) -> list[dict[str, Any]]:
        result = []
        for distance, item in enumerate(rows_newest_first):
            score = scorer.score(
                item,
                rank=distance,
                candidate_text=item.get("message_text") or str(item.get("payload") or ""),
                candidate_world_id=scorer.context.world_id,
                causal_distance=distance,
            )
            event = dict(item)
            event["tier"] = 0 if distance < 3 else 1
            event["relevance"] = score.as_dict()
            result.append(event)
        result.sort(key=lambda x: (x["tier"], -x["relevance"]["total"]))
        return result
