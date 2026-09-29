from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.research import CharacterResearchService
from .lifecycle import ActionRecord, CharacterAgencyStore
from .policy import ActionPolicyService
from .gateway import ExternalGateway


ActionHandler = Callable[[UUID, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    schema: dict[str, Any]
    side_effect_class: str
    allowed_worker_classes: frozenset[str]
    handler: ActionHandler | None
    execution_mode: str = "local"
    result_mode: str = "final"
    capability_class: str = "action"
    description: str = ""
    required_authority_use: str | None = None


class ActionRegistry:
    def __init__(self):
        self._specs: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Action '{spec.name}' is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ActionSpec | None:
        return self._specs.get(name)

    def capabilities_for(self, worker_class: str) -> dict[str, dict[str, Any]]:
        """Operator/runtime capability metadata; not an inference prompt contract."""
        return {
            name: {
                "class": spec.capability_class,
                "description": spec.description,
                "side_effect_class": spec.side_effect_class,
                "result_mode": spec.result_mode,
                "execution_mode": spec.execution_mode,
                "schema": spec.schema,
                "required_authority_use": spec.required_authority_use,
            }
            for name, spec in self._specs.items()
            if not spec.allowed_worker_classes or worker_class in spec.allowed_worker_classes
        }


class ActionDispatcher:
    def __init__(self, db: Database, registry: ActionRegistry):
        self.db = db
        self.registry = registry
        self.store = CharacterAgencyStore(db)

    async def _authority_precondition_satisfied(
        self, action: ActionRecord, required_use: str
    ) -> bool:
        """Execution-time authority gate for consequential actions."""
        if action.task_id is None:
            return False
        row = await self.db.fetchrow(
            """SELECT 1
               FROM aios.character_cognitive_task task
               JOIN aios.cognitive_evidence_instances(task.instance_id) eligible ON true
               JOIN aios.knowledge_acquisition_event kae ON kae.instance_id=eligible.instance_id
               JOIN aios.epistemic_authority_admission eaa ON eaa.acquisition_id=kae.acquisition_id
               WHERE task.task_id=$1
                 AND $2=ANY(eaa.authorized_uses)
                 AND (task.source_node_id IS NULL
                      OR kae.dag_node_id=task.source_node_id
                      OR kae.dag_node_id=task.source_through_node_id)
               LIMIT 1""",
            action.task_id, required_use,
        )
        return row is not None

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

        if spec.required_authority_use:
            if not await self._authority_precondition_satisfied(action, spec.required_authority_use):
                return await self.store.transition_action(
                    action_id, "rejected", rejection_reason="epistemic_precondition_unsatisfied"
                )

        policy = ActionPolicyService(self.db)
        disposition = await policy.disposition(action.instance_id, action.action_type, action.side_effect_class)
        if disposition == "deny":
            return await self.store.transition_action(action_id, "rejected", rejection_reason="action_policy_denied")
        if disposition == "require_approval" and not await policy.is_approved(action_id):
            await policy.request_approval(action_id)
            return await self.store.transition_action(action_id, "waiting")

        await self.db.execute(
            """UPDATE aios.character_action
               SET result_mode=$2, execution_mode=$3
               WHERE action_id=$1""",
            action_id, spec.result_mode, spec.execution_mode,
        )
        if action.status == "waiting":
            action = await self.store.transition_action(action_id, "validated")
        elif action.status == "proposed":
            action = await self.store.transition_action(action_id, "validated")
        if spec.execution_mode == "worker":
            return await self.store.transition_action(action_id, "queued")
        if spec.handler is None:
            return await self.store.transition_action(
                action_id, "failed", error="local action has no handler"
            )
        action = await self.store.transition_action(action_id, "running")
        try:
            handler_args = dict(action.arguments)
            # Internal dispatch context is not part of the model-visible action
            # schema or persisted arguments. Handlers may use it for per-worker
            # budgets without trusting model-supplied metadata.
            handler_args["_worker_class"] = worker_class
            result = await spec.handler(action.instance_id, handler_args)
        except Exception as exc:
            return await self.store.transition_action(
                action_id, "failed", error=str(exc)[:2000]
            )
        result = dict(result)
        delivery = result.get("external_delivery")
        if isinstance(delivery, dict):
            delivery_id = await ExternalGateway(self.db).queue_delivery(
                action_id=action_id,
                integration_key=str(delivery["integration_key"]),
                payload=dict(delivery.get("payload") or {}),
            )
            result["delivery_id"] = str(delivery_id)
            result["delivery_status"] = "pending"
        return await self.store.transition_action(action_id, "succeeded", result=result)


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
        schema={"type":"object","required":["query"],"properties":{
            "query":{"type":"string"},"limit":{"type":"integer"}},"additionalProperties":False},
        side_effect_class="read_only",
        allowed_worker_classes=frozenset({"executive","research","planning"}),
        handler=corpus_search,
        capability_class="research",
        description="Legacy corpus-specific research search.",
    ))
    registry.register(ActionSpec(
        name="corpus.acquire",
        schema={"type":"object","required":["section_ids"],"properties":{
            "section_ids":{"type":"array"},"mode":{"type":"string"}},"additionalProperties":False},
        side_effect_class="internal_write",
        allowed_worker_classes=frozenset({"executive","research"}),
        handler=corpus_acquire,
        capability_class="research",
        description="Explicitly acquire selected corpus evidence into durable character knowledge.",
    ))

    from .adapters import register_external_actions, register_interagent_actions
    from .cognitive_actions import register_cognitive_actions
    from .capabilities import register_agent_capabilities
    register_cognitive_actions(db, registry)
    register_agent_capabilities(db, registry)
    register_external_actions(db, registry)
    register_interagent_actions(db, registry)
    return registry
