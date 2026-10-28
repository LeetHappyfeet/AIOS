"""Versioned workspace client boundary; one source-writing binding per instance.

This does not grant filesystem access: the client owns its local execution boundary.
All routes require the operator's separate AIOS_WORKSPACE_API_KEY.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def public(row):
    return {
        k: decode(v) if k == "result" else str(v) if isinstance(v, UUID) else v
        for k, v in dict(row).items()
    }


class BindingIn(BaseModel):
    workspace_key: str = Field(min_length=1, max_length=160)
    worker_key: str = Field(min_length=1, max_length=160)
    character_id: str = Field(min_length=1, max_length=160)
    instance_id: UUID
    session_id: UUID
    user_name: str = Field(min_length=1, max_length=160)
    scope_key: str = Field(min_length=1, max_length=160)
    root_hint: str = Field(min_length=1, max_length=1000)


class AssignmentIn(BaseModel):
    request_id: UUID
    objective: str = Field(min_length=1, max_length=16000)


class ClaimIn(BaseModel):
    worker_key: str


class LeaseIn(ClaimIn):
    claim_token: UUID


class ObservationIn(LeaseIn):
    event_key: UUID
    text: str = Field(min_length=1, max_length=24000)
    payload: dict[str, Any] = Field(default_factory=dict)


class FinishIn(LeaseIn):
    status: Literal["succeeded", "failed", "cancelled", "interrupted"]
    result: dict[str, Any] = Field(default_factory=dict)


class WorkspaceStore:
    def __init__(self, db):
        self.db = db

    async def instances(self, character_id):
        rows = await self.db.fetch(
            """SELECT ci.character_id, rs.instance_id, t.session_id, t.user_name, t.scope_key,
                      rs.source_head_node_id, rs.source_timeline_id
               FROM aios.character_runtime_state rs
               JOIN aios.character_instance ci USING(instance_id)
               JOIN aios.timeline t ON t.timeline_id=rs.timeline_id
               WHERE ci.character_id=$1 AND t.session_id IS NOT NULL
               ORDER BY rs.instance_id LIMIT 100""",
            character_id,
        )
        return [public(row) for row in rows]

    async def binding(self, binding_id, worker_key=None, lock=False):
        row = await self.db.fetchrow(
            "SELECT * FROM aios.workspace_binding WHERE binding_id=$1"
            + (" FOR UPDATE" if lock else ""),
            binding_id,
        )
        if not row or (worker_key is not None and row["worker_key"] != worker_key):
            raise HTTPException(404, "Unknown workspace binding")
        return row

    async def current(self, binding):
        row = await self.db.fetchrow(
            "SELECT source_timeline_id,source_head_node_id FROM aios.character_runtime_state WHERE instance_id=$1 FOR UPDATE",
            binding["instance_id"],
        )
        if (
            not row
            or row["source_timeline_id"] != binding["timeline_id"]
            or row["source_head_node_id"] != binding["last_node_id"]
        ):
            raise HTTPException(
                409,
                "Character source advanced elsewhere; reconcile the binding before continuing",
            )
        return row

    async def bind(self, req):
        async with self.db.atomic():
            row = await self.db.fetchrow(
                """SELECT ci.character_id,rs.timeline_id,rs.source_timeline_id,
                rs.source_head_node_id,t.session_id,t.user_name,t.scope_key,
                st.character_id AS source_character_id,st.session_id AS source_session_id,
                st.user_name AS source_user_name,st.scope_key AS source_scope_key
                FROM aios.character_runtime_state rs JOIN aios.character_instance ci USING(instance_id)
                JOIN aios.timeline t ON t.timeline_id=rs.timeline_id
                JOIN aios.timeline st ON st.timeline_id=rs.source_timeline_id
                WHERE rs.instance_id=$1 FOR UPDATE OF rs""",
                req.instance_id,
            )
            if not row or any(
                row[k] != getattr(req, k)
                for k in ("character_id", "session_id", "user_name", "scope_key")
            ):
                raise HTTPException(
                    409,
                    "Character, instance, session, user, and scope must match an existing runtime",
                )
            if any(
                row["source_" + k] != getattr(req, k)
                for k in ("character_id", "session_id", "user_name", "scope_key")
            ):
                raise HTTPException(
                    409, "Source timeline does not match the requested session binding"
                )
            old = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_binding WHERE instance_id=$1 OR workspace_key=$2",
                req.instance_id,
                req.workspace_key,
            )
            if old:
                for k in (
                    "workspace_key",
                    "worker_key",
                    "instance_id",
                    "character_id",
                    "session_id",
                    "user_name",
                    "scope_key",
                    "root_hint",
                ):
                    if old[k] != getattr(req, k):
                        raise HTTPException(
                            409,
                            "Workspace binding already belongs to a different profile",
                        )
                return public(old)
            new = await self.db.execute_returning_row(
                """INSERT INTO aios.workspace_binding
                (workspace_key,worker_key,instance_id,character_id,session_id,user_name,scope_key,timeline_id,last_node_id,root_hint)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *""",
                req.workspace_key,
                req.worker_key,
                req.instance_id,
                req.character_id,
                req.session_id,
                req.user_name,
                req.scope_key,
                row["source_timeline_id"],
                row["source_head_node_id"],
                req.root_hint,
            )
            return public(new)

    async def submit(self, binding_id, req):
        from .actions import ActionDispatcher, default_action_registry
        from .lifecycle import CharacterAgencyStore
        from .policy import ActionPolicyService

        async with self.db.atomic():
            b = await self.binding(binding_id, lock=True)
            await self.current(b)
            old = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_run WHERE binding_id=$1 AND request_id=$2",
                binding_id,
                req.request_id,
            )
            if old:
                if old["objective"] != req.objective:
                    raise HTTPException(
                        409, "Request ID reused with different objective"
                    )
                return public(old)
            action = await CharacterAgencyStore(self.db).create_action(
                instance_id=b["instance_id"],
                action_type="workspace.run",
                arguments={"binding_id": str(binding_id), "objective": req.objective},
                side_effect_class="external_sensitive",
                idempotency_key=f"workspace:{binding_id}:{req.request_id}",
                proposed_by="workspace_operator",
            )
            # This authenticated endpoint is an explicit operator assignment;
            # approve just this action, preserving deny policies and other actions.
            policy = ActionPolicyService(self.db)
            await policy.request_approval(action.action_id)
            await policy.decide(
                action.action_id,
                True,
                "workspace_operator",
                "Explicit workspace assignment",
            )
            action = await ActionDispatcher(
                self.db, default_action_registry(self.db)
            ).dispatch(action.action_id, worker_class="executive")
            if action.status != "queued":
                raise HTTPException(
                    409, f"Workspace action was not admitted: {action.status}"
                )
            row = await self.db.execute_returning_row(
                """INSERT INTO aios.workspace_run(binding_id,action_id,request_id,objective)
                VALUES($1,$2,$3,$4) RETURNING *""",
                binding_id,
                action.action_id,
                req.request_id,
                req.objective,
            )
            return public(row)

    async def claim(self, binding_id, worker_key):
        async with self.db.atomic():
            await self.binding(binding_id, worker_key, lock=True)
            # Never replay an abandoned coding run: shell side effects may have happened.
            expired = await self.db.fetch(
                """UPDATE aios.workspace_run SET status='interrupted',updated_at=now(),
                result='{"reason":"lease expired; inspect before resubmitting"}'::jsonb
                WHERE binding_id=$1 AND status='running' AND lease_expires_at<=now() RETURNING action_id""",
                binding_id,
            )
            for old in expired:
                await self.db.execute(
                    "UPDATE aios.character_action SET status='timed_out',completed_at=now(),updated_at=now() WHERE action_id=$1 AND status='running'",
                    old["action_id"],
                )
            if await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM aios.workspace_run WHERE binding_id=$1 AND status='running')",
                binding_id,
            ):
                return None
            row = await self.db.execute_returning_row(
                """UPDATE aios.workspace_run SET status='running',claim_token=$2,
                lease_expires_at=now()+interval '120 seconds',updated_at=now()
                WHERE run_id=(SELECT r.run_id FROM aios.workspace_run r JOIN aios.character_action a USING(action_id)
                    WHERE r.binding_id=$1 AND r.status='queued' AND a.status='queued'
                    ORDER BY r.created_at,r.run_id LIMIT 1 FOR UPDATE OF r SKIP LOCKED) RETURNING *""",
                binding_id,
                uuid4(),
            )
            if row:
                await self.db.execute(
                    "UPDATE aios.character_action SET status='running',started_at=COALESCE(started_at,now()),updated_at=now() WHERE action_id=$1 AND status='queued'",
                    row["action_id"],
                )
            return public(row) if row else None

    async def owned(self, binding_id, run_id, req, terminal=False):
        b = await self.binding(binding_id, req.worker_key, lock=True)
        row = await self.db.fetchrow(
            "SELECT *, lease_expires_at>now() AS lease_live FROM aios.workspace_run WHERE run_id=$1 AND binding_id=$2 FOR UPDATE",
            run_id,
            binding_id,
        )
        if not row or row["claim_token"] != req.claim_token:
            raise HTTPException(409, "Obsolete or foreign claim")
        if row["status"] != "running":
            if terminal:
                return b, row
            raise HTTPException(409, "Run is no longer active")
        if not row["lease_live"]:
            raise HTTPException(409, "Workspace lease expired")
        action_status = await self.db.fetchval(
            "SELECT status FROM aios.character_action WHERE action_id=$1 FOR UPDATE",
            row["action_id"],
        )
        if action_status != "running":
            raise HTTPException(409, "The underlying action was stopped or changed")
        return b, row

    async def renew(self, binding_id, run_id, req):
        async with self.db.atomic():
            b, _ = await self.owned(binding_id, run_id, req)
            await self.current(b)
            await self.db.execute(
                "UPDATE aios.workspace_run SET lease_expires_at=now()+interval '120 seconds',updated_at=now() WHERE run_id=$1",
                run_id,
            )
            return {"ok": True}

    async def observe(self, binding_id, run_id, req):
        from aios_app.ingest_api import ingest_message
        from aios_app.models import IngestIn

        if len(json.dumps(req.payload)) > 30000:
            raise HTTPException(422, "Observation payload too large")
        digest = hashlib.sha256(
            json.dumps(
                {"text": req.text, "payload": req.payload}, sort_keys=True
            ).encode()
        ).hexdigest()
        async with self.db.atomic():
            b, run = await self.owned(binding_id, run_id, req)
            old = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_receipt WHERE run_id=$1 AND event_key=$2",
                run_id,
                req.event_key,
            )
            if old:
                if old["payload_hash"] != digest:
                    raise HTTPException(409, "Event ID reused with different content")
                return decode(old["response"])
            await self.current(b)
            result = await ingest_message(
                self.db,
                IngestIn(
                    session_id=b["session_id"],
                    character_id=b["character_id"],
                    user_name=b["user_name"],
                    scope_key=b["scope_key"],
                    speaker_type="tool",
                    speaker_id=f"workspace:{b['binding_id']}",
                    recipient_id=b["character_id"],
                    kind="tool_result",
                    text=req.text,
                    payload={
                        **req.payload,
                        "source": "aios-client",
                        "workspace_binding_id": str(binding_id),
                        "workspace_run_id": str(run_id),
                    },
                    dedupe_key=f"workspace:{run_id}:{req.event_key}",
                ),
            )
            if not result.source_current or result.timeline_id != b["timeline_id"]:
                raise HTTPException(
                    409, "Observation did not advance the bound character source"
                )
            response = result.model_dump(mode="json")
            await self.db.execute(
                "UPDATE aios.workspace_binding SET last_node_id=$2 WHERE binding_id=$1",
                binding_id,
                result.node_id,
            )
            await self.db.execute(
                "INSERT INTO aios.workspace_receipt VALUES($1,$2,$3,$4::jsonb)",
                run_id,
                req.event_key,
                digest,
                json.dumps(response),
            )
            return response

    async def finish(self, binding_id, run_id, req):
        from .runtime import AgentRuntimeStore

        if len(json.dumps(req.result)) > 60000:
            raise HTTPException(422, "Result exceeds size limit")
        async with self.db.atomic():
            b, row = await self.owned(binding_id, run_id, req, terminal=True)
            if row["status"] != "running":
                if row["status"] == req.status and decode(row["result"]) == req.result:
                    return public(row)
                raise HTTPException(409, "Run already ended with a different result")
            row = await self.db.execute_returning_row(
                "UPDATE aios.workspace_run SET status=$2,result=$3::jsonb,updated_at=now() WHERE run_id=$1 RETURNING *",
                run_id,
                req.status,
                json.dumps(req.result),
            )
            action_status = {"interrupted": "timed_out"}.get(req.status, req.status)
            await self.db.execute(
                "UPDATE aios.character_action SET status=$2,result=$3::jsonb,completed_at=now(),updated_at=now() WHERE action_id=$1",
                row["action_id"],
                action_status,
                json.dumps(req.result),
            )
            await AgentRuntimeStore(self.db).wake(
                instance_id=b["instance_id"],
                event_type="WORKSPACE_RUN_FINISHED",
                source_type="workspace",
                source_id=str(run_id),
                payload={
                    "run_id": str(run_id),
                    "status": req.status,
                    "result": req.result,
                    "verification": "worker_report",
                },
                dedupe_key=f"workspace-finish:{run_id}",
            )
            return public(row)


def install_workspace_routes(app, db, hud_handler):
    async def authorize(authorization: str = Header(default="")):
        key = os.environ.get("AIOS_WORKSPACE_API_KEY", "")
        if not key:
            raise HTTPException(
                503, "Set AIOS_WORKSPACE_API_KEY to enable workspace clients"
            )
        if not hmac.compare_digest(authorization, "Bearer " + key):
            raise HTTPException(401, "Invalid workspace API key")

    router = APIRouter(prefix="/workspace-client", dependencies=[Depends(authorize)])
    store = WorkspaceStore(db)

    @router.get("/protocol")
    async def protocol():
        return {
            "version": 1,
            "capabilities": ["workspace.run", "observations", "lease-renewal"],
            "single_writer": True,
        }

    @router.get("/instances")
    async def instances(character_id: str):
        return {"instances": await store.instances(character_id)}

    @router.post("/bind")
    async def bind(req: BindingIn):
        return await store.bind(req)

    @router.post("/{binding_id}/context")
    async def context(binding_id: UUID):
        async with db.atomic():
            b = await store.binding(binding_id, lock=True)
            await store.current(b)
            node = b["last_node_id"]
            instance = b["instance_id"]
        if node is None:
            raise HTTPException(
                409, "Bound instance requires an initial source observation"
            )
        return await hud_handler(
            instance_id=instance,
            through_node_id=node,
            recent_limit=8,
            token_budget=2500,
            wait_ms=2500,
        )

    @router.post("/{binding_id}/submit")
    async def submit(binding_id: UUID, req: AssignmentIn):
        return await store.submit(binding_id, req)

    @router.post("/{binding_id}/claim")
    async def claim(binding_id: UUID, req: ClaimIn):
        return {"run": await store.claim(binding_id, req.worker_key)}

    @router.get("/{binding_id}/runs/{run_id}")
    async def status(binding_id: UUID, run_id: UUID):
        await store.binding(binding_id)
        row = await db.fetchrow(
            "SELECT * FROM aios.workspace_run WHERE binding_id=$1 AND run_id=$2",
            binding_id,
            run_id,
        )
        if not row:
            raise HTTPException(404, "Unknown run")
        return public(row)

    @router.post("/{binding_id}/runs/{run_id}/renew")
    async def renew(binding_id: UUID, run_id: UUID, req: LeaseIn):
        return await store.renew(binding_id, run_id, req)

    @router.post("/{binding_id}/runs/{run_id}/observe")
    async def observe(binding_id: UUID, run_id: UUID, req: ObservationIn):
        return await store.observe(binding_id, run_id, req)

    @router.post("/{binding_id}/runs/{run_id}/finish")
    async def finish(binding_id: UUID, run_id: UUID, req: FinishIn):
        return await store.finish(binding_id, run_id, req)

    app.include_router(router)
