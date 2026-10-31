"""Versioned workspace client boundary; explicit writer or read-only attachment.

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

from aios_app.launch_config import get_workspace_api_key

from .environment_links import (
    EnvironmentStore,
    LinkIn,
    MemorySharingIn,
    ProgressIn,
    RecallIn,
    append_event,
)
from .workspace_setup import WorkspaceSetup, ExecutionIn
from .workspace_registrations import RegistrationStore, RegistrationIn, AuthorizationIn
from .workspace_contract import Assignment, verification_status
from .work_admission import WorkAdmission, WorkRequestIn, ProfileIn, WorkDecision, WorkDismissal


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def public(row):
    return {
        k: decode(v)
        if k
        in {
            "result",
            "assignment",
            "decision",
            "payload",
            "assignment_template",
            "profile_snapshot",
            "intent",
        }
        else str(v)
        if isinstance(v, UUID)
        else v
        for k, v in dict(row).items()
        if k != "lease_live"
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


class AssignmentIn(Assignment):
    request_id: UUID

    def spec(self):
        return Assignment.model_validate(
            {k: getattr(self, k) for k in Assignment.model_fields}
        )


class DelegationIn(AssignmentIn):
    source_node_id: UUID


class ClaimIn(BaseModel):
    worker_key: str


class ClaimRequest(ClaimIn):
    protocol_version: Literal[2, 3] = 2


class LeaseIn(ClaimIn):
    claim_token: UUID


class ObservationIn(LeaseIn):
    event_key: UUID
    text: str = Field(min_length=1, max_length=24000)
    payload: dict[str, Any] = Field(default_factory=dict)


class FinishIn(LeaseIn):
    status: Literal["finished", "succeeded", "failed", "cancelled", "interrupted"]
    result: dict[str, Any] = Field(default_factory=dict)


class ReviewIn(BaseModel):
    accept: bool
    reviewer: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


class WorkspaceStore:
    def __init__(self, db):
        self.db = db

    async def instances(self, character_id):
        rows = await self.db.fetch(
            """SELECT ci.character_id, rs.instance_id, COALESCE(ci.current_world_id,ci.world_id) AS world_id, t.session_id, t.user_name, t.scope_key,
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
            or (not binding['attached'] and row["source_head_node_id"] != binding["last_node_id"])
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
                            f"Workspace binding {old['binding_id']} already belongs to a different profile; "
                            f"mismatched field: {k}. Restore its saved identity or use persistent registration.",
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

    async def submit(
        self, binding_id, req, *, environment=None, operator_assignment=True
    ):
        from .actions import ActionDispatcher, default_action_registry
        from .lifecycle import CharacterAgencyStore
        from .policy import ActionPolicyService

        async with self.db.atomic():
            b = await self.binding(binding_id, lock=True)
            await self.current(b)
            if b['attached'] and environment is None:
                raise HTTPException(409, "Attached workspace requires operator-linked v3 work admission")
            old = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_run WHERE binding_id=$1 AND request_id=$2",
                binding_id,
                req.request_id,
            )
            if old:
                if (
                    old["environment_link_id"] is not None
                    or old["assignment_hash"] != req.spec().digest()
                ):
                    raise HTTPException(
                        409, "Request ID reused with different assignment"
                    )
                return public(old)
            spec = req.spec()
            goal_owner = b
            if environment:
                goal_owner = {
                    "instance_id": environment["origin_instance_id"],
                    "timeline_id": environment["origin_source_timeline_id"],
                }
            if spec.goal_id:
                await self.open_goal(goal_owner, spec)
            action = await CharacterAgencyStore(self.db).create_action(
                instance_id=b["instance_id"],
                action_type="workspace.run",
                arguments={"binding_id": str(binding_id), "objective": req.objective},
                side_effect_class="external_sensitive",
                idempotency_key=f"workspace:{binding_id}:{req.request_id}",
                proposed_by="workspace_operator"
                if operator_assignment
                else "aios_work_admission",
            )
            # Explicit operator submissions can approve this one action. Model work
            # uses policy admission without manufacturing an approval.
            policy = ActionPolicyService(self.db)
            if operator_assignment:
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
                """INSERT INTO aios.workspace_run(binding_id,action_id,request_id,objective,assignment,assignment_hash)
                VALUES($1,$2,$3,$4,$5::jsonb,$6) RETURNING *""",
                binding_id,
                action.action_id,
                req.request_id,
                req.objective,
                json.dumps(spec.model_dump(mode="json")),
                spec.digest(),
            )
            if environment:
                row = await self.db.execute_returning_row(
                    """UPDATE aios.workspace_run SET environment_link_id=$2,origin_instance_id=$3,
                       origin_source_timeline_id=$4,origin_source_node_id=$5,required_protocol_version=3
                       WHERE run_id=$1 RETURNING *""",
                    row["run_id"],
                    environment["link_id"],
                    environment["origin_instance_id"],
                    environment["origin_source_timeline_id"],
                    req.source_node_id,
                )
            await append_event(
                self.db, row["run_id"], "host:queued", "queued", "Assignment queued"
            )
            return public(row)

    async def claim(self, binding_id, worker_key, protocol_version=2):
        async with self.db.atomic():
            await self.binding(binding_id, worker_key, lock=True)
            # Never replay an abandoned coding run: shell side effects may have happened.
            expired = await self.db.fetch(
                """UPDATE aios.workspace_run SET status='interrupted',updated_at=now(),
                result='{"reason":"lease expired; inspect before resubmitting"}'::jsonb
                WHERE binding_id=$1 AND status='running' AND lease_expires_at<=now() RETURNING action_id,run_id""",
                binding_id,
            )
            for old in expired:
                await append_event(
                    self.db,
                    old["run_id"],
                    "host:expired",
                    "interrupted",
                    "Worker lease expired; execution outcome unknown",
                )
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
                    AND r.required_protocol_version<=$3 AND NOT r.cancel_requested
                    AND (r.environment_link_id IS NULL OR EXISTS(
                      SELECT 1 FROM aios.character_environment_link l WHERE l.link_id=r.environment_link_id AND l.enabled))
                    ORDER BY r.created_at,r.run_id LIMIT 1 FOR UPDATE OF r SKIP LOCKED) RETURNING *""",
                binding_id,
                uuid4(),
                protocol_version,
            )
            if row:
                await self.db.execute(
                    "UPDATE aios.character_action SET status='running',started_at=COALESCE(started_at,now()),updated_at=now() WHERE action_id=$1 AND status='queued'",
                    row["action_id"],
                )
                await append_event(
                    self.db,
                    row["run_id"],
                    "host:started",
                    "started",
                    "Worker claimed assignment",
                )
            return public(row) if row else None

    async def owned(self, binding_id, run_id, req, terminal=False, stopping=False):
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
        if row["cancel_requested"] and not terminal and not stopping:
            raise HTTPException(409, "Cancellation requested")
        if row["environment_link_id"] is not None and not terminal:
            link = await EnvironmentStore(self.db).link(
                binding_id, row["environment_link_id"], enabled=False
            )
            # Renewal delivers a stop request after revocation; no new work is authorized.
            if link["enabled"]:
                await EnvironmentStore(self.db).origin(
                    link, ancestor_node_id=row["origin_source_node_id"]
                )
            elif not row["cancel_requested"]:
                raise HTTPException(409, "Environment link revoked")
        return b, row

    async def renew(self, binding_id, run_id, req):
        async with self.db.atomic():
            b, run = await self.owned(binding_id, run_id, req, stopping=True)
            await self.current(b)
            if run["required_protocol_version"] >= 3:
                await self.db.execute(
                    "UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1",
                    binding_id,
                )
            await self.db.execute(
                "UPDATE aios.workspace_run SET lease_expires_at=now()+interval '120 seconds',updated_at=now() WHERE run_id=$1",
                run_id,
            )
            return {"ok": True, "cancel_requested": run["cancel_requested"]}

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
            if run["cancel_requested"]:
                raise HTTPException(
                    409,
                    "Cancellation requested; finish the run without new observations",
                )
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
            if b['attached']:
                if run['environment_link_id'] is None or run['origin_instance_id'] != b['instance_id']:
                    raise HTTPException(409, "Attached observation requires a linked run")
                sequence = await append_event(
                    self.db, run_id, f"observation:{req.event_key}", "observation", req.text,
                    {"source": "aios-client", "workspace_binding_id": str(binding_id),
                     "workspace_run_id": str(run_id), "text": req.text, "payload": req.payload})
                response = {"ok": True, "source": "workspace-run", "run_id": str(run_id),
                            "sequence": sequence, "source_current": False}
                await self.db.execute(
                    "INSERT INTO aios.workspace_receipt VALUES($1,$2,$3,$4::jsonb)",
                    run_id, req.event_key, digest, json.dumps(response))
                return response
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

    async def open_goal(self, binding, spec):
        row = await self.db.fetchrow(
            """SELECT a.attempt_id FROM aios.character_goal_attempt a
            JOIN aios.character_agent_goal g ON g.goal_id=a.goal_id
            WHERE a.attempt_id=$1 AND a.goal_id=$2 AND a.instance_id=$3
              AND a.source_timeline_id=$4 AND a.closed_at IS NULL
              AND g.instance_id=$3 AND g.status IN ('active','scheduled','dormant')
            FOR UPDATE OF a,g""",
            spec.attempt_id,
            spec.goal_id,
            binding["instance_id"],
            binding["timeline_id"],
        )
        if not row:
            raise HTTPException(
                409,
                "Goal attempt is closed, foreign, or from a different source timeline",
            )

    async def finish(self, binding_id, run_id, req):
        from .runtime import AgentRuntimeStore

        if len(json.dumps(req.result)) > 60000:
            raise HTTPException(422, "Result exceeds size limit")
        async with self.db.atomic():
            b, row = await self.owned(binding_id, run_id, req, terminal=True)
            spec_data = decode(row["assignment"])
            spec = Assignment.model_validate(
                spec_data or {"objective": row["objective"]}
            )
            result = {
                **req.result,
                "verification": "worker_evidence",
                "verification_status": verification_status(
                    spec, req.result.get("evidence")
                ),
                "goal_review": "pending" if spec.goal_id else "not_linked",
            }
            # Finishing execution is never an assertion of goal completion.
            status = "finished" if req.status == "succeeded" else req.status
            if row["cancel_requested"]:
                status = "cancelled"
            if result["verification_status"] == "failed" and status == "finished":
                status = "failed"
            if row["status"] != "running":
                if row["status"] == status and decode(row["result"]) == result:
                    return public(row)
                raise HTTPException(409, "Run already ended with a different result")
            row = await self.db.execute_returning_row(
                "UPDATE aios.workspace_run SET status=$2,result=$3::jsonb,updated_at=now() WHERE run_id=$1 RETURNING *",
                run_id,
                status,
                json.dumps(result),
            )
            action_status = {"interrupted": "timed_out", "finished": "succeeded"}.get(
                status, status
            )
            await self.db.execute(
                "UPDATE aios.character_action SET status=$2,result=$3::jsonb,completed_at=now(),updated_at=now() WHERE action_id=$1",
                row["action_id"],
                action_status,
                json.dumps(result),
            )
            if spec.goal_id:
                await self.db.execute(
                    "INSERT INTO aios.workspace_review(run_id,goal_id,attempt_id) VALUES($1,$2,$3)",
                    run_id,
                    spec.goal_id,
                    spec.attempt_id,
                )
            await AgentRuntimeStore(self.db).wake(
                instance_id=b["instance_id"],
                event_type="WORKSPACE_RUN_FINISHED",
                source_type="workspace",
                source_id=str(run_id),
                payload={
                    "run_id": str(run_id),
                    "status": status,
                    "result": result,
                    "verification": "worker_evidence",
                    "goal_completion": "requires_review",
                },
                dedupe_key=f"workspace-finish:{run_id}",
            )
            if row["origin_instance_id"] is not None and row["origin_instance_id"] != b['instance_id']:
                await AgentRuntimeStore(self.db).wake(
                    instance_id=row["origin_instance_id"],
                    event_type="WORKSPACE_RUN_FINISHED",
                    source_type="workspace",
                    source_id=str(run_id),
                    payload={
                        "run_id": str(run_id),
                        "status": status,
                        "verification_status": result["verification_status"],
                        "evidence_ref": f"workspace:{run_id}",
                        "goal_completion": "requires_review",
                    },
                    dedupe_key=f"workspace-finish:{run_id}",
                )
            await append_event(
                self.db,
                run_id,
                "host:finished",
                status,
                str(result.get("report") or result.get("error") or "Execution ended")[
                    :1000
                ],
                {
                    "verification_status": result["verification_status"],
                    "goal_review": result["goal_review"],
                },
            )
            return public(row)

    async def review(self, binding_id, run_id, req):
        from aios_app.epistemic.goals import CharacterGoalService

        async with self.db.atomic():
            b = await self.binding(binding_id, lock=True)
            row = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_run WHERE binding_id=$1 AND run_id=$2 FOR UPDATE",
                binding_id,
                run_id,
            )
            review = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_review WHERE run_id=$1 FOR UPDATE",
                run_id,
            )
            if not row or not review:
                raise HTTPException(404, "No goal-linked evidence to review")
            decision = req.model_dump()
            if review["status"] != "pending":
                if decode(review["decision"]) == decision:
                    return public(review)
                raise HTTPException(409, "Evidence already reviewed")
            spec = Assignment.model_validate(decode(row["assignment"]))
            if req.accept:
                await self.current(b)
                goal_owner = b
                if row["environment_link_id"] is not None:
                    link = await EnvironmentStore(self.db).link(
                        binding_id, row["environment_link_id"]
                    )
                    await EnvironmentStore(self.db).origin(
                        link, ancestor_node_id=row["origin_source_node_id"]
                    )
                    goal_owner = {
                        "instance_id": row["origin_instance_id"],
                        "timeline_id": row["origin_source_timeline_id"],
                    }
                if row["cancel_requested"]:
                    raise HTTPException(
                        409, "Cancelled assignment cannot complete a goal"
                    )
                await self.open_goal(goal_owner, spec)
                result = decode(row["result"])
                if (
                    row["status"] != "finished"
                    or verification_status(spec, result.get("evidence")) != "passed"
                ):
                    raise HTTPException(
                        409, "Cannot accept incomplete, stale, or failed checks"
                    )
                await CharacterGoalService(self.db).finish(
                    instance_id=goal_owner["instance_id"],
                    goal_id=spec.goal_id,
                    attempt_id=spec.attempt_id,
                    status="completed",
                    verification="reviewed",
                    resolution_kind="workspace_operator_review",
                    source_node_id=row["origin_source_node_id"] or b["last_node_id"],
                    evidence_ids=[f"workspace:{run_id}"],
                    refresh_scene=False,
                )
            reviewed = await self.db.execute_returning_row(
                """UPDATE aios.workspace_review SET status=$2,decision=$3::jsonb,reviewed_at=now()
                   WHERE run_id=$1 RETURNING *""",
                run_id,
                "accepted" if req.accept else "rejected",
                json.dumps(decision),
            )
            await append_event(
                self.db,
                run_id,
                "operator:review",
                "reviewed",
                "Goal evidence accepted" if req.accept else "Goal evidence rejected",
                {"decision": "accepted" if req.accept else "rejected"},
            )
            return public(reviewed)


def install_workspace_routes(app, db, hud_handler):
    async def authorize(authorization: str = Header(default="")):
        key = get_workspace_api_key()
        if not key:
            raise HTTPException(
                503, "Save a workspace key in Gradio Workspace Access or set AIOS_WORKSPACE_API_KEY"
            )
        if not hmac.compare_digest(authorization, "Bearer " + key):
            raise HTTPException(401, "Invalid workspace API key")

    router = APIRouter(prefix="/workspace-client", dependencies=[Depends(authorize)])
    store = WorkspaceStore(db)

    @router.get("/protocol")
    async def protocol():
        return {
            "version": 2,
            "supported_versions": [2, 3],
            "environment_protocol": 3,
            "capabilities": [
                "workspace.run",
                "observations",
                "lease-renewal",
                "assignments",
                "verification",
                "goal-review",
                "environment-links-v3",
                "work-admission-v1",
                "delegation-readiness-v1",
                "workspace-attachment-v1",
                "persistent-workspaces-v1",
                "character-session-discovery-v1",
                "delegated-runs-v3",
                "character-recall-v3",
                "task-events-v3",
            ],
            "single_writer": True,
        }

    @router.get("/instances")
    async def instances(character_id: str):
        return {"instances": await store.instances(character_id)}

    @router.get("/characters")
    async def characters():
        return {"characters": await WorkspaceSetup(db).characters()}

    @router.get("/sessions")
    async def sessions(character_id: str):
        # Exact runtime/session/user/scope coordinates, rather than session guesses.
        return {"sessions": await store.instances(character_id)}

    @router.get("/v3/readiness")
    async def readiness(origin_instance_id: UUID, binding_id: UUID | None = None):
        return await WorkspaceSetup(db).readiness(origin_instance_id, binding_id)

    @router.get("/v3/bindings")
    async def bindings(character_id: str):
        return {"bindings": await WorkspaceSetup(db).bindings(character_id)}

    registrations = RegistrationStore(db)

    @router.post("/registrations")
    async def register_workspace(req: RegistrationIn):
        return await registrations.register(req)

    @router.post("/registrations/{registration_id}/claim")
    async def claim_registered_workspace(registration_id: UUID, req: ClaimIn):
        return await registrations.claim(registration_id, req.worker_key)

    @router.post("/bind")
    async def bind(req: BindingIn):
        return await store.bind(req)

    @router.post("/{binding_id}/context")
    async def context(binding_id: UUID):
        async with db.atomic():
            b = await store.binding(binding_id, lock=True)
            current = await store.current(b)
            node = current['source_head_node_id'] if b['attached'] else b["last_node_id"]
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
    async def claim(binding_id: UUID, req: ClaimRequest):
        b = await store.binding(binding_id, req.worker_key)
        if b['registration_id']:
            raise HTTPException(409, 'This workspace uses automatic chat routing; listen through its registration')
        if req.protocol_version == 3:
            await db.execute(
                "UPDATE aios.workspace_binding SET last_seen_at=now() WHERE binding_id=$1",
                binding_id,
            )

            await WorkAdmission(db).reconsider(binding_id=binding_id)
        return {
            "run": await store.claim(binding_id, req.worker_key, req.protocol_version)
        }

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

    # New work is explicitly capability/version gated; v2 clients never claim it.
    @router.post("/v3/{binding_id}/runs/{run_id}/context")
    async def execution_context(binding_id: UUID, run_id: UUID, req: LeaseIn):
        async with db.atomic():
            b, run = await store.owned(binding_id, run_id, req)
            current = await store.current(b)
            node = current['source_head_node_id'] if b['attached'] else b['last_node_id']
            if run["cancel_requested"]:
                raise HTTPException(409, "Cancellation requested")
            if node is None:
                raise HTTPException(409, "Workspace needs an initial observation")
        return await hud_handler(
            instance_id=b["instance_id"],
            through_node_id=node,
            recent_limit=4,
            token_budget=2500,
            wait_ms=2500,
            profile_name="workspace_execution",
            focus_text=run["objective"],
        )

    @router.post("/v3/{binding_id}/runs/{run_id}/recall")
    async def recall(binding_id: UUID, run_id: UUID, req: RecallIn):
        return await EnvironmentStore(db).recall(binding_id, run_id, req)

    @router.post("/v3/{binding_id}/runs/{run_id}/progress")
    async def progress(binding_id: UUID, run_id: UUID, req: ProgressIn):
        return await EnvironmentStore(db).progress(binding_id, run_id, req)

    @router.get("/v3/{binding_id}/runs/{run_id}/events")
    async def events(binding_id: UUID, run_id: UUID, after: int = 0):
        await store.binding(binding_id)
        run = await db.fetchrow(
            "SELECT run_id FROM aios.workspace_run WHERE binding_id=$1 AND run_id=$2",
            binding_id,
            run_id,
        )
        if not run:
            raise HTTPException(404, "Unknown run")
        rows = await db.fetch(
            "SELECT * FROM aios.workspace_run_event WHERE run_id=$1 AND sequence>$2 ORDER BY sequence LIMIT 100",
            run_id,
            max(0, after),
        )
        return {
            "events": [public(r) for r in rows],
            "next_cursor": int(rows[-1]["sequence"]) if rows else max(0, after),
        }

    async def authorize_review(authorization: str = Header(default="")):
        key = os.environ.get("AIOS_WORKSPACE_REVIEW_KEY", "")
        if not key or key == get_workspace_api_key():
            raise HTTPException(
                503, "Set a distinct AIOS_WORKSPACE_REVIEW_KEY for human review"
            )
        if not hmac.compare_digest(authorization, "Bearer " + key):
            raise HTTPException(401, "Invalid review API key")

    reviews = APIRouter(
        prefix="/workspace-client", dependencies=[Depends(authorize_review)]
    )

    @reviews.get("/registrations")
    async def list_registrations():
        return {"registrations": await registrations.listing()}

    @reviews.post("/registrations/{registration_id}/authorization")
    async def authorize_registration(registration_id: UUID, req: AuthorizationIn):
        return await registrations.authorize(registration_id, req)

    @reviews.post("/v3/execution-instances")
    async def create_execution(req: ExecutionIn):
        return await WorkspaceSetup(db).create_execution(req)

    @reviews.get("/work/{instance_id}")
    async def pending_work(instance_id: UUID):
        rows = await db.fetch(
            "SELECT * FROM aios.character_work_request WHERE instance_id=$1 ORDER BY created_at DESC LIMIT 100",
            instance_id)
        return {"requests": [public(r) for r in rows]}

    @reviews.post("/v3/{binding_id}/links/{link_id}/work-profiles")
    async def configure_work(binding_id: UUID, link_id: UUID, req: ProfileIn):
        return await WorkAdmission(db).configure(binding_id, link_id, req)

    @reviews.get("/work/{instance_id}/{request_id}")
    async def inspect_work(instance_id: UUID, request_id: UUID):
        return await WorkAdmission(db).inspect(instance_id, request_id)

    @reviews.post("/work/{instance_id}/{request_id}/decision")
    async def decide_work(instance_id: UUID, request_id: UUID, req: WorkDecision):
        return await WorkAdmission(db).decide(instance_id, request_id, req)

    @reviews.post("/work/{instance_id}/{request_id}/dismiss")
    async def dismiss_work(instance_id: UUID, request_id: UUID, req: WorkDismissal):
        return await WorkAdmission(db).dismiss(instance_id, request_id, req)

    async def authorize_request(authorization: str = Header(default="")):
        key = os.environ.get("AIOS_WORK_REQUEST_API_KEY", "")
        if not key or key in {
            os.environ.get("AIOS_WORKSPACE_REVIEW_KEY"),
            get_workspace_api_key(),
        }:
            raise HTTPException(
                503, "Set a distinct AIOS_WORK_REQUEST_API_KEY for native work requests"
            )
        if not hmac.compare_digest(authorization, "Bearer " + key):
            raise HTTPException(401, "Invalid work request API key")

    requests = APIRouter(
        prefix="/agent/instance", dependencies=[Depends(authorize_request)]
    )

    @requests.post("/{instance_id}/work/request")
    async def request_work(instance_id: UUID, req: WorkRequestIn):
        return await WorkAdmission(db).request(instance_id, req)

    app.include_router(requests)

    @reviews.post("/v3/{binding_id}/links")
    async def link_environment(binding_id: UUID, req: LinkIn):
        return await EnvironmentStore(db).create(binding_id, req)

    @reviews.post("/v3/{binding_id}/links/{link_id}/memory")
    async def set_link_memory(binding_id: UUID, link_id: UUID, req: MemorySharingIn):
        return await EnvironmentStore(db).set_memory(binding_id, link_id, req)

    @reviews.post("/v3/{binding_id}/links/{link_id}/revoke")
    async def revoke_environment(binding_id: UUID, link_id: UUID):
        return await EnvironmentStore(db).revoke(binding_id, link_id)

    @reviews.post("/v3/{binding_id}/links/{link_id}/submit")
    async def delegate(binding_id: UUID, link_id: UUID, req: DelegationIn):
        return await EnvironmentStore(db).delegate(binding_id, link_id, req)

    @reviews.post("/v3/{binding_id}/runs/{run_id}/cancel")
    async def cancel(binding_id: UUID, run_id: UUID):
        return await EnvironmentStore(db).cancel(binding_id, run_id)

    @reviews.get("/{binding_id}/runs/{run_id}/review")
    async def review_bundle(binding_id: UUID, run_id: UUID):
        await store.binding(binding_id)
        row = await db.fetchrow(
            "SELECT * FROM aios.workspace_run WHERE binding_id=$1 AND run_id=$2",
            binding_id,
            run_id,
        )
        if not row:
            raise HTTPException(404, "Unknown workspace run")
        decision = await db.fetchrow(
            "SELECT * FROM aios.workspace_review WHERE run_id=$1", run_id
        )
        return {"run": public(row), "review": public(decision) if decision else None}

    @reviews.post("/{binding_id}/runs/{run_id}/review")
    async def review(binding_id: UUID, run_id: UUID, req: ReviewIn):
        return await store.review(binding_id, run_id, req)

    app.include_router(reviews)
    app.include_router(router)
