from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from .actions import ActionRegistry, ActionSpec


def register_external_actions(db: Database, registry: ActionRegistry) -> None:
    async def n8n_workflow(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"external_delivery": {
            "integration_key": str(args.get("integration_key") or "n8n"),
            "payload": {"kind": "n8n_workflow", "workflow": str(args["workflow"]),
                        "input": dict(args.get("input") or {})},
        }}

    async def email_draft(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"draft": {"to": str(args["to"]), "subject": str(args.get("subject") or ""),
                          "body": str(args["body"]), "thread_id": args.get("thread_id")}}

    async def email_send(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"external_delivery": {
            "integration_key": str(args.get("integration_key") or "email"),
            "payload": {"kind": "email_send", "message": {
                "to": str(args["to"]), "subject": str(args.get("subject") or ""),
                "body": str(args["body"]), "thread_id": args.get("thread_id")}},
        }}

    registry.register(ActionSpec(
        "n8n.run_workflow",
        {"type":"object","required":["workflow"],"properties":{
            "workflow":{"type":"string"},"input":{"type":"object"},
            "integration_key":{"type":"string"}},"additionalProperties":False},
        "external_sensitive", frozenset({"executive","planning","communication"}), n8n_workflow,
    ))
    registry.register(ActionSpec(
        "email.draft",
        {"type":"object","required":["to","body"],"properties":{
            "to":{"type":"string"},"subject":{"type":"string"},"body":{"type":"string"},
            "thread_id":{"type":"string"}},"additionalProperties":False},
        "internal_write", frozenset({"executive","communication"}), email_draft,
    ))
    registry.register(ActionSpec(
        "email.send",
        {"type":"object","required":["to","body"],"properties":{
            "to":{"type":"string"},"subject":{"type":"string"},"body":{"type":"string"},
            "thread_id":{"type":"string"},"integration_key":{"type":"string"}},
         "additionalProperties":False},
        "external_sensitive", frozenset({"executive","communication"}), email_send,
    ))


def register_interagent_actions(db: Database, registry: ActionRegistry) -> None:
    from .runtime import AgentRuntimeStore
    runtime = AgentRuntimeStore(db)

    async def send_message(instance_id: UUID, args: Mapping[str, Any]) -> Mapping[str, Any]:
        target = UUID(str(args["target_instance_id"]))
        row = await db.fetchrow("SELECT instance_id FROM aios.character_instance WHERE instance_id=$1", target)
        if not row:
            raise LookupError("target character instance does not exist")
        wake_id = await runtime.wake(
            instance_id=target, event_type="AGENT_MESSAGE_RECEIVED",
            source_type="character_instance", source_id=str(instance_id),
            payload={"from_instance_id":str(instance_id),"message":str(args["message"])},
            dedupe_key=str(args.get("message_id") or f"{instance_id}:{args['message']}"),
        )
        return {"target_instance_id":str(target),"wake_id":str(wake_id),"delivered":True}

    registry.register(ActionSpec(
        "agent.send_message",
        {"type":"object","required":["target_instance_id","message"],"properties":{
            "target_instance_id":{"type":"string"},"message":{"type":"string"},
            "message_id":{"type":"string"}},"additionalProperties":False},
        "internal_write", frozenset({"executive","communication"}), send_message,
    ))
