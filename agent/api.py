from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from aios_app.agent.gateway import ExternalGateway
from aios_app.agent.policy import ActionPolicyService
from aios_app.agent.actions import ActionDispatcher, default_action_registry
from aios_app.agent.lifecycle import CharacterAgencyStore
from aios_app.agent.remote_workers import RemoteWorkerStore


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


class RemoteWorkerIn(BaseModel):
    worker_key: str = Field(min_length=1)
    worker_type: str = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    labels: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int = Field(default=1, ge=1, le=256)


class RemoteWorkerClaimIn(BaseModel):
    lease_seconds: int = Field(default=120, ge=30, le=3600)


class RemoteWorkerResultIn(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)


class RemoteWorkerFailureIn(BaseModel):
    error: str = Field(min_length=1, max_length=2000)


def _action_payload(action) -> dict[str, Any]:
    return {
        "action_id": str(action.action_id),
        "instance_id": str(action.instance_id),
        "task_id": str(action.task_id or ""),
        "action_type": action.action_type,
        "arguments": action.arguments,
        "side_effect_class": action.side_effect_class,
        "result_mode": action.result_mode,
        "execution_mode": action.execution_mode,
        "status": action.status,
        "lease_expires_at": str(action.lease_expires_at or ""),
    }


def install_external_agency_routes(app, db) -> None:
    @app.post("/agent/workers")
    async def register_remote_worker(req: RemoteWorkerIn):
        row = await RemoteWorkerStore(db).register(**req.model_dump())
        return {
            "worker_id": str(row["worker_id"]),
            "worker_key": row["worker_key"],
            "worker_type": row["worker_type"],
            "capabilities": list(row["capabilities"] or []),
            "max_concurrency": row["max_concurrency"],
            "status": row["status"],
        }

    @app.post("/agent/workers/{worker_id}/heartbeat")
    async def heartbeat_remote_worker(worker_id: UUID):
        row = await RemoteWorkerStore(db).heartbeat(worker_id)
        return {"worker_id": str(row["worker_id"]), "status": row["status"]}

    @app.post("/agent/workers/{worker_id}/claim")
    async def claim_remote_work(worker_id: UUID, req: RemoteWorkerClaimIn):
        action = await RemoteWorkerStore(db).claim(
            worker_id, lease_seconds=req.lease_seconds
        )
        return {"action": _action_payload(action) if action else None}

    @app.post("/agent/workers/{worker_id}/action/{action_id}/complete")
    async def complete_remote_work(
        worker_id: UUID, action_id: UUID, req: RemoteWorkerResultIn
    ):
        action = await RemoteWorkerStore(db).complete(worker_id, action_id, req.result)
        return _action_payload(action)

    @app.post("/agent/workers/{worker_id}/action/{action_id}/fail")
    async def fail_remote_work(
        worker_id: UUID, action_id: UUID, req: RemoteWorkerFailureIn
    ):
        action = await RemoteWorkerStore(db).fail(worker_id, action_id, req.error)
        return _action_payload(action)

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
