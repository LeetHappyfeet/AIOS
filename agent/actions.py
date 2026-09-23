from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.research import CharacterResearchService
from .lifecycle import ActionRecord, CharacterAgencyStore
from .policy import ActionPolicyService


ActionHandler = Callable[[UUID, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    schema: dict[str, Any]
    side_effect_class: str
    allowed_worker_classes: frozenset[str]
    handler: ActionHandler


class ActionRegistry:
    def __init__(self):
        self._specs: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Action '{spec.name}' is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ActionSpec | None:
        return self._specs.get(name)

    def schemas_for(self, worker_class: str) -> dict[str, dict[str, Any]]:
        return {
            name: spec.schema for name, spec in self._specs.items()
            if not spec.allowed_worker_classes or worker_class in spec.allowed_worker_classes
        }


class ActionDispatcher:
    def __init__(self, db: Database, registry: ActionRegistry):
        self.db = db
        self.registry = registry
        self.store = CharacterAgencyStore(db)

    async def dispatch(self, action_id: UUID, *, worker_class: str) -> ActionRecord:
        action = await self.store.get_action(action_id)
        if not action:
            raise LookupError(f"Unknown action {action_id}")
        spec = self.registry.get(action.action_type)
        if not spec:
            return await self.store.transition_action(
                action_id, "rejected", rejection_reason="unknown_action"
            )
        if spec.allowed_worker_classes and worker_class not in spec.allowed_worker_classes:
            return await self.store.transition_action(
                action_id, "rejected", rejection_reason="worker_class_not_allowed"
            )
        if action.side_effect_class != spec.side_effect_class:
            return await self.store.transition_action(
                action_id, "rejected", rejection_reason="side_effect_class_mismatch"
            )
        if action.expected_state_version is not None:
            row = await self.db.fetchrow(
                "SELECT state_version FROM aios.character_runtime_state WHERE instance_id=$1",
                action.instance_id,
            )
            if not row or int(row["state_version"]) != int(action.expected_state_version):
                return await self.store.transition_action(
                    action_id, "rejected", rejection_reason="stale_character_state"
                )

        policy = ActionPolicyService(self.db)
        disposition = await policy.disposition(action.instance_id, action.action_type, action.side_effect_class)
        if disposition == "deny":
            return await self.store.transition_action(action_id, "rejected", rejection_reason="action_policy_denied")
        if disposition == "require_approval" and not await policy.is_approved(action_id):
            await policy.request_approval(action_id)
            return await self.store.transition_action(action_id, "waiting")

        action = await self.store.transition_action(action_id, "validated")
        action = await self.store.transition_action(action_id, "running")
        try:
            result = await spec.handler(action.instance_id, action.arguments)
        except Exception as exc:
            return await self.store.transition_action(
                action_id, "failed", error=str(exc)[:2000]
            )
        return await self.store.transition_action(
            action_id, "succeeded", result=dict(result)
        )


def default_action_registry(db: Database) -> ActionRegistry:
    registry = ActionRegistry()
    research = CharacterResearchService(db)

    async def corpus_search(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        result = await research.search(
            instance_id=instance_id,
            query=str(args["query"]),
            limit=int(args.get("limit", 8)),
        )
        return {
            "research_id": str(result.research_id),
            "status": result.status,
            "query": result.query,
            "hits": [
                {
                    "section_id": str(hit.section_id),
                    "document_id": str(hit.document_id),
                    "score": hit.score,
                    "title": hit.title,
                    "heading": hit.heading,
                    "excerpt": hit.excerpt,
                    "scopes": list(hit.scopes),
                }
                for hit in result.hits
            ],
        }

    async def corpus_acquire(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        section_ids = [UUID(str(value)) for value in args["section_ids"]]
        result = await research.acquire(
            instance_id=instance_id,
            section_ids=section_ids,
            mode=str(args.get("mode", "research")),
        )
        return {
            **result,
            "instance_id": str(result.get("instance_id", instance_id)),
            "consumption_ids": [str(v) for v in result.get("consumption_ids", [])],
        }

    registry.register(ActionSpec(
        name="corpus.search",
        schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        side_effect_class="read_only",
        allowed_worker_classes=frozenset({"executive","research","planning"}),
        handler=corpus_search,
    ))
    registry.register(ActionSpec(
        name="corpus.acquire",
        schema={
            "type": "object",
            "required": ["section_ids"],
            "properties": {
                "section_ids": {"type": "array"},
                "mode": {"type": "string"},
            },
            "additionalProperties": False,
        },
        side_effect_class="internal_write",
        allowed_worker_classes=frozenset({"executive","research"}),
        handler=corpus_acquire,
    ))
    return registry
