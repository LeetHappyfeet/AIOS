from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.weights import get_profile
from aios_app.hud.context import HUDContext, HUDContextResolver
from aios_app.hud.relevance import HUDRelevanceScorer


@dataclass(frozen=True)
class HUDBudget:
    """Approximate token caps.  These are selection budgets, not tokenizer guarantees."""

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
    # Deliberately tokenizer-independent.  Four characters/token is conservative
    # enough for bounded prompt assembly and can later be replaced by a model
    # tokenizer without changing the assembler contract.
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
            # Keep one highly ranked item rather than making a section vanish.
            clipped = dict(item)
            text = text_fn(item)
            clipped["text"] = text[: max(64, token_cap * 4)]
            selected.append(clipped)
            break
        selected.append(item)
        used += cost
    return selected


class HUDAssembler:
    """
    Build the canonical prompt-ready RPG HUD.

    The assembler has two hard boundaries:
      * HUDContextResolver decides branch/world eligibility.
      * Context Resolver classifications decide semantic routing.

    Relevance only ranks candidates that have already crossed those boundaries.
    """

    def __init__(self, db: Database, *, budget: Optional[HUDBudget] = None):
        self.db = db
        self.context_resolver = HUDContextResolver(db)
        self.budget = budget or HUDBudget()

    async def build(
        self,
        instance_id: UUID,
        *,
        recent_limit: int = 12,
        token_budget: Optional[int] = None,
    ) -> dict[str, Any]:
        context = await self.context_resolver.resolve(instance_id)
        raw_state = await self._runtime_state(instance_id)

        recent_rows = await self.db.fetch(
            """
            SELECT node_id, event_time, speaker_id, speaker_role, message_text, payload
            FROM aios.dag_node
            WHERE timeline_id=$1
            ORDER BY event_id DESC
            LIMIT $2
            """,
            context.timeline_id,
            max(1, min(recent_limit, 100)),
        )
        recent_newest = [dict(row) for row in recent_rows]
        focus_text = next(
            (row.get("message_text") for row in recent_newest if row.get("message_text")),
            "",
        )
        goals = list(_json_value(raw_state.get("goals"), []))
        scorer = HUDRelevanceScorer(context, focus_text=focus_text, goals=goals)

        section_caps = dict(self.budget.section_tokens)
        if token_budget is not None and token_budget > 0:
            scale = token_budget / max(1, self.budget.total_tokens)
            section_caps = {
                key: max(32, int(value * scale))
                for key, value in section_caps.items()
            }
            resolved_total = token_budget
        else:
            resolved_total = self.budget.total_tokens

        identity = await self._identity(context)
        scene = await self._scene(
            context,
            scorer,
            token_cap=section_caps["scene"],
        )
        relationships = await self._relationships(context, scorer)
        inventory = await self._inventory(context, scorer)
        knowledge = await self._knowledge(context, scorer)
        rules = await self._rules(context, scorer)
        recent_events = self._recent_events(recent_newest, scorer)

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
            for goal in goals
        ] + semantic_goals

        # Runtime rules are authoritative constraints.  Claim-derived RULE items
        # are character knowledge and remain visibly marked as epistemic.
        rule_items = rules + [
            {**item, "source": "character_knowledge"}
            for item in semantic_rules
        ]

        # Old event claims can be useful causal context, but current DAG events
        # stay first because they describe what actually just happened here.
        event_items = list(reversed(recent_events)) + semantic_events

        memories = _trim_to_budget(
            memories,
            section_caps["memories"],
            lambda x: x.get("text", ""),
        )
        beliefs = _trim_to_budget(
            beliefs,
            section_caps["beliefs"],
            lambda x: x.get("text", ""),
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
            goal_items,
            section_caps["goals"],
            lambda x: x.get("text", ""),
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

        return {
            "identity": identity,
            "presence": {
                "instance_id": context.instance_id,
                "entity_id": context.entity_id,
                "world_id": context.world_id,
                "world_key": context.world_key,
                "timeline_id": context.timeline_id,
                "head_node_id": context.head_node_id,
                "state_version": context.state_version,
                "lifecycle_state": context.lifecycle_state,
                "location_entity_id": context.location_entity_id,
            },
            "scene": scene,
            "state": {
                "health": raw_state.get("health"),
                "stamina": raw_state.get("stamina"),
                "energy": raw_state.get("energy"),
                "physical": _json_value(raw_state.get("physical_state"), {}),
                "emotional": _json_value(raw_state.get("emotional_state"), {}),
                "social": _json_value(raw_state.get("social_state"), {}),
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
            "actions": ["speak", "move", "inspect", "use_item", "wait", "custom"],
            "hud": {
                "version": "hud-v1",
                "selection": "branch-aware/entity-centered/deterministic",
                "token_budget": resolved_total,
                "section_token_budgets": section_caps,
                "world_lineage": list(context.lineage_world_ids),
                "focus_text": focus_text,
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
            self.db,
            character_id=context.character_id,
        )
        return identity

    async def _scene(
        self,
        context: HUDContext,
        scorer: HUDRelevanceScorer,
        *,
        token_cap: int,
    ) -> dict[str, Any]:
        seed_ids = list(context.scene_entity_ids)
        rows = await self.db.fetch(
            """
            WITH seed(entity_id) AS (
                SELECT unnest($3::uuid[])
            ),
            expanded(entity_id) AS (
                SELECT entity_id FROM seed
                UNION
                SELECT CASE
                    WHEN r.subject_entity_id = ANY($3::uuid[]) THEN r.object_entity_id
                    ELSE r.subject_entity_id
                END
                FROM aios.world_entity_relation r
                WHERE r.world_id=$1
                  AND r.valid_to_node_id IS NULL
                  AND (
                      r.subject_entity_id = ANY($3::uuid[])
                      OR r.object_entity_id = ANY($3::uuid[])
                  )
            )
            SELECT e.entity_id, e.entity_type, e.display_name, e.entity_key, e.meta
            FROM aios.world_entity e
            JOIN expanded x ON x.entity_id=e.entity_id
            WHERE e.world_id=$1
              AND e.entity_id IS DISTINCT FROM $2
            ORDER BY e.created_at
            LIMIT 200
            """,
            context.world_id,
            context.entity_id,
            seed_ids,
        )
        entities: list[dict[str, Any]] = []
        location = None
        for rank, row in enumerate(rows):
            item = dict(row)
            item["tier"] = 0 if context.entity_is_active(item["entity_id"]) else 2
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
            key=lambda item: (
                item.get("tier", 9),
                -item["relevance"]["total"],
            )
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
            item for item in entities
            if str(item.get("entity_type", "")).lower() in {"character", "person", "agent", "user"}
        ]
        objects = [item for item in entities if item not in actors and item is not location]
        return {
            "location": location,
            "actors": actors,
            "objects": objects,
            "relations": [dict(row) for row in relations],
        }

    async def _relationships(
        self,
        context: HUDContext,
        scorer: HUDRelevanceScorer,
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
        self,
        context: HUDContext,
        scorer: HUDRelevanceScorer,
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

    async def _knowledge(
        self,
        context: HUDContext,
        scorer: HUDRelevanceScorer,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT
                ck.epistemic_status,
                ck.confidence,
                ck.acquisition_mode,
                ck.source_entity_id,
                ck.updated_at,
                ck.base_confidence,
                ck.attention_weight,
                ck.trust_weight,
                ck.compatibility_weight,
                ck.retention_weight,
                ck.salience_weight,
                ck.effective_confidence,
                p.proposition_id,
                p.topic_key,
                p.canonical_text,
                p.subject_norm,
                p.predicate_norm,
                p.object_norm,
                p.polarity,
                p.modality,
                COALESCE(ctx.claim_kind, 'BELIEF') AS claim_kind,
                ctx.predicate_family,
                ctx.world_id AS source_world_id,
                ctx.dag_node_id AS source_node_id,
                COALESCE(conflicts.items, '[]'::jsonb) AS conflicts
            FROM aios.character_proposition_knowledge ck
            JOIN aios.proposition p ON p.proposition_id=ck.proposition_id
            LEFT JOIN LATERAL (
                SELECT
                    ccr.claim_kind,
                    ccr.predicate_family,
                    ccr.world_id,
                    ccr.dag_node_id
                FROM aios.observation o
                JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
                WHERE o.proposition_id=p.proposition_id
                ORDER BY
                    (ccr.character_instance_id=$1) DESC,
                    ccr.resolved_at DESC
                LIMIT 1
            ) ctx ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'proposition_id', other.proposition_id,
                        'text', other.canonical_text,
                        'conflict_type', pc.conflict_type,
                        'strength', pc.strength
                    )
                ) AS items
                FROM aios.proposition_conflict pc
                JOIN aios.proposition other
                  ON other.proposition_id = CASE
                      WHEN pc.proposition_a_id=p.proposition_id THEN pc.proposition_b_id
                      ELSE pc.proposition_a_id
                  END
                JOIN aios.character_proposition_knowledge other_ck
                  ON other_ck.instance_id=$1
                 AND other_ck.proposition_id=other.proposition_id
                WHERE pc.proposition_a_id=p.proposition_id
                   OR pc.proposition_b_id=p.proposition_id
            ) conflicts ON true
            WHERE ck.instance_id=$1
            ORDER BY ck.updated_at DESC
            LIMIT 250
            """,
            context.instance_id,
        )

        result = []
        for rank, row in enumerate(rows):
            item = dict(row)
            item["text"] = item.pop("canonical_text")
            item["conflicts"] = _json_value(item.get("conflicts"), [])
            # Explicit instance acquisition is the branch-crossing authorization.
            # source_world_id therefore informs scoring/debugging but does not
            # reject knowledge already owned by this current character instance.
            score = scorer.score(
                item,
                rank=rank,
                candidate_text=(
                    f"{item.get('topic_key','')} {item.get('subject_norm','')} "
                    f"{item.get('predicate_norm','')} {item.get('object_norm','')} {item.get('text','')}"
                ),
                candidate_world_id=context.world_id,
                candidate_entity_id=item.get("source_entity_id"),
                epistemic_status=item.get("epistemic_status"),
                confidence=item.get("effective_confidence") or item.get("confidence"),
                updated_at=item.get("updated_at"),
            )
            kind = str(item.get("claim_kind") or "BELIEF").upper()
            item["tier"] = self._knowledge_tier(kind, score.total)
            item["relevance"] = score.as_dict()
            result.append(item)

        result.sort(key=lambda x: (x["tier"], -x["relevance"]["total"]))
        return result

    async def _rules(
        self,
        context: HUDContext,
        scorer: HUDRelevanceScorer,
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
        result.sort(key=lambda x: (x["tier"], x.get("priority", 100), -x["relevance"]["total"]))
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

    @staticmethod
    def _knowledge_tier(kind: str, score: float) -> int:
        if kind in {"GOAL", "RULE"}:
            return 0
        if kind in {"MEMORY", "RELATIONSHIP", "STATE", "EVENT"} and score >= 1.0:
            return 1
        return 2
