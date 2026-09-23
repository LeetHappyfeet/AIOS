from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from aios_app.agent.gateway import ExternalGateway
from aios_app.agent.policy import ActionPolicyService
from aios_app.agent.actions import ActionDispatcher, default_action_registry
from aios_app.agent.lifecycle import CharacterAgencyStore


class IntegrationIn(BaseModel):
    integration_key: str
    integration_type: str
    display_name: str
    base_url: str | None = None
    auth_env: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class ExternalEventIn(BaseModel):
    external_event_id: str
    instance_id: UUID
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ActionPolicyIn(BaseModel):
    disposition: str
    actor: str = "operator"


class ApprovalIn(BaseModel):
    approved: bool
    actor: str = "operator"
    reason: str | None = None
    worker_class: str = "executive"


def install_external_agency_routes(app, db) -> None:
    @app.post("/agent/integrations")
    async def register_integration(req: IntegrationIn):
        return await ExternalGateway(db).register(**req.model_dump())

    @app.post("/agent/integrations/{integration_key}/event")
    async def ingest_external_event(integration_key: str, req: ExternalEventIn):
        receipt = await ExternalGateway(db).ingest_event(
            integration_key=integration_key, **req.model_dump()
        )
        return {"receipt_id": str(receipt)}

    @app.put("/agent/instance/{instance_id}/policy/{action_type:path}")
    async def set_action_policy(instance_id: UUID, action_type: str, req: ActionPolicyIn):
        await ActionPolicyService(db).set_policy(
            instance_id, action_type, req.disposition, req.actor
        )
        return {"updated": True}

    @app.post("/agent/action/{action_id}/approval")
    async def decide_action(action_id: UUID, req: ApprovalIn):
        policy = ActionPolicyService(db)
        await policy.decide(action_id, req.approved, req.actor, req.reason)
        if not req.approved:
            action = await CharacterAgencyStore(db).transition_action(
                action_id, "rejected", rejection_reason="operator_denied"
            )
        else:
            action = await ActionDispatcher(db, default_action_registry(db)).dispatch(
                action_id, worker_class=req.worker_class
            )
        return {"action_id": str(action.action_id), "status": action.status}

    @app.post("/agent/delivery/{delivery_id}/run")
    async def run_external_delivery(delivery_id: UUID):
        return await ExternalGateway(db).deliver(delivery_id)
