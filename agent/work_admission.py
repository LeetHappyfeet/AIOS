"""Purpose-based asynchronous work requests. AIOS owns destination and authority.

The model may propose intent, never a binding, credential or verification command.
Native callers and source-markup callers enter the same idempotent admission path.
"""

from __future__ import annotations

import hashlib
import json
import re
import logging
from uuid import UUID, uuid5, NAMESPACE_URL

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .workspace_contract import Assignment


class WorkIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    purpose: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    objective: str = Field(min_length=3, max_length=4000)
    constraints: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("objective")
    @classmethod
    def objective_text(cls, value):
        if not value.strip():
            raise ValueError("Objective must not be blank")
        return value.strip()

    @field_validator("constraints")
    @classmethod
    def bounded_constraints(cls, values):
        if any(not x.strip() or len(x) > 200 for x in values):
            raise ValueError(
                "Constraints must be nonempty text of at most 200 characters"
            )
        return values


class WorkRequestIn(WorkIntent):
    request_id: UUID = Field(strict=False)
    source_node_id: UUID = Field(strict=False)

    def intent(self):
        return WorkIntent.model_validate(
            self.model_dump(exclude={"request_id", "source_node_id"})
        )


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    purpose: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=3, max_length=300)
    # Template is the existing assignment schema, with objective/goal chosen by AIOS.
    assignment_template: dict
    enabled: bool = True

    @field_validator("assignment_template")
    @classmethod
    def template(cls, value):
        if set(value) & {
            "objective",
            "goal_id",
            "attempt_id",
            "request_id",
            "source_node_id",
        }:
            raise ValueError("Template cannot supply objective or identity coordinates")
        spec = Assignment.model_validate(
            {"objective": "Operator execution profile", **value}
        )
        if not spec.required_checks:
            raise ValueError("A work profile must define required host checks")
        return spec.model_dump(
            mode="json", exclude={"objective", "goal_id", "attempt_id"}
        )


class WorkDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept: bool
    reviewer: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)


class WorkDismissal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reviewer", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Reviewer and dismissal reason must not be blank")
        return value.strip()


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def tool_schema():
    parameters = WorkIntent.model_json_schema()
    parameters.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": "request_work",
            "description": "Request bounded work by capability and purpose. AIOS chooses the authorized destination. Returns task admission, not completed work. Do not resubmit pending work.",
            "parameters": parameters,
        },
    }


_ACTION = re.compile(r"<aios_action>(.*?)</aios_action>", re.I | re.S)


def extract_work_intent(text):
    source = str(text or "")
    matches = []
    for match in _ACTION.finditer(source):
        prefix = source[: match.start()]
        fence = None
        for line in prefix.splitlines():
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if marker:
                char = marker.group(1)[0]
                fence = None if fence == char else char if fence is None else fence
        line_prefix = prefix.rsplit("\n", 1)[-1]
        if (
            fence is not None
            or line_prefix.lstrip().startswith(">")
            or line_prefix.count("`") % 2
        ):
            continue
        matches.append(match.group(1))
    if not matches:
        return None
    if len(matches) != 1 or len(matches[0]) > 8000:
        raise ValueError(
            "Use exactly one bounded structured action per character reply"
        )
    raw = json.loads(matches[0])
    if not isinstance(raw, dict) or raw.get("op") != "request_work":
        return None
    return WorkIntent.model_validate({k: v for k, v in raw.items() if k != "op"})


class WorkAdmission:
    def __init__(self, db):
        self.db = db

    async def source(self, instance_id, node_id, *, character=False):
        row = await self.db.fetchrow(
            """SELECT rs.source_timeline_id,rs.source_head_node_id,ci.character_id,
                     dn.speaker_role::text AS role,dn.speaker_id,dn.message_text,ie.viewpoint_id
               FROM aios.character_runtime_state rs JOIN aios.character_instance ci USING(instance_id)
               JOIN aios.dag_node dn ON dn.node_id=$2 AND dn.timeline_id=rs.source_timeline_id
                 AND dn.node_id=rs.source_head_node_id
               JOIN aios.ingest_event ie ON ie.event_id=dn.event_id AND ie.superseded_at IS NULL
               WHERE rs.instance_id=$1""",
            instance_id,
            node_id,
        )
        if not row or (
            character
            and (
                row["role"] != "character"
                or row["speaker_id"] != row["character_id"]
                or str(row["viewpoint_id"]) != row["character_id"]
            )
        ):
            raise HTTPException(
                409,
                "Work request requires the current source and actual character authorship",
            )
        return row

    async def configure(self, binding_id, link_id, req):
        from .environment_links import EnvironmentStore
        from .workspaces import WorkspaceStore, public

        async with self.db.atomic():
            await WorkspaceStore(self.db).binding(binding_id, lock=True)
            await EnvironmentStore(self.db).link(binding_id, link_id)
            # An edited profile never silently broadens existing pending/approved work.
            old = await self.db.fetchrow(
                "SELECT * FROM aios.workspace_work_profile WHERE link_id=$1 AND capability=$2 AND purpose=$3 FOR UPDATE",
                link_id,
                req.capability,
                req.purpose,
            )
            if old:
                if decode(old["assignment_template"]) != req.assignment_template:
                    raise HTTPException(
                        409,
                        "Profile criteria are immutable; configure a new purpose or link",
                    )
                return public(
                    await self.db.execute_returning_row(
                        "UPDATE aios.workspace_work_profile SET enabled=$2,description=$3 WHERE profile_id=$1 RETURNING *",
                        old["profile_id"],
                        req.enabled,
                        req.description,
                    )
                )
            return public(
                await self.db.execute_returning_row(
                    """INSERT INTO aios.workspace_work_profile(link_id,capability,purpose,description,assignment_template,enabled)
                   VALUES($1,$2,$3,$4,$5::jsonb,$6) RETURNING *""",
                    link_id,
                    req.capability,
                    req.purpose,
                    req.description,
                    json.dumps(req.assignment_template),
                    req.enabled,
                )
            )

    async def request(self, instance_id, req, *, character_source=False):
        intent = req.intent().model_dump()
        digest = fingerprint(intent)
        async with self.db.atomic():
            # Resolve retries first; later chat turns must not duplicate an admitted job.
            await self.db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"work-request:{instance_id}",
            )
            alias = await self.db.fetchrow(
                "SELECT * FROM aios.work_request_alias WHERE request_id=$1",
                req.request_id,
            )
            if alias:
                if (
                    alias["instance_id"] != instance_id
                    or alias["source_node_id"] != req.source_node_id
                    or alias["intent_hash"] != digest
                ):
                    raise HTTPException(
                        409, "Request ID reused with different work or identity"
                    )
                old = await self.db.fetchrow(
                    "SELECT * FROM aios.character_work_request WHERE request_id=$1",
                    alias["canonical_request_id"],
                )
                return self.receipt(old)
            old = await self.db.fetchrow(
                "SELECT * FROM aios.character_work_request WHERE request_id=$1 FOR UPDATE",
                req.request_id,
            )
            if old is None:
                old = await self.db.fetchrow(
                    "SELECT * FROM aios.character_work_request WHERE instance_id=$1 AND source_node_id=$2 AND intent_hash=$3 FOR UPDATE",
                    instance_id,
                    req.source_node_id,
                    digest,
                )
            if old:
                if (
                    old["instance_id"] != instance_id
                    or old["source_node_id"] != req.source_node_id
                    or old["intent_hash"] != digest
                ):
                    raise HTTPException(
                        409, "Request ID reused with different work or identity"
                    )
                await self.alias(req, instance_id, digest, old["request_id"])
                return self.receipt(old)
            source = await self.source(
                instance_id, req.source_node_id, character=character_source
            )
            from .workspace_registrations import RegistrationStore
            await RegistrationStore(self.db).ensure_available(instance_id)
            duplicate = await self.db.fetchrow(
                """SELECT r.* FROM aios.character_work_request r LEFT JOIN aios.workspace_run w USING(run_id)
                   WHERE r.instance_id=$1 AND r.source_timeline_id=$2 AND r.intent_hash=$3
                     AND r.status NOT IN ('denied','invalidated')
                     AND (r.run_id IS NULL OR w.status IN ('queued','running'))
                   ORDER BY r.created_at LIMIT 1 FOR UPDATE OF r""",
                instance_id,
                source["source_timeline_id"],
                digest,
            )
            if duplicate:
                await self.alias(req, instance_id, digest, duplicate["request_id"])
                return self.receipt(duplicate)
            goals = await self.db.fetch(
                """SELECT g.goal_id,a.attempt_id FROM aios.character_agent_goal g JOIN aios.character_goal_attempt a USING(goal_id)
                   WHERE g.instance_id=$1 AND a.instance_id=$1 AND g.status='active'
                     AND a.closed_at IS NULL AND a.source_timeline_id=$2 LIMIT 2""",
                instance_id,
                source["source_timeline_id"],
            )
            goal = dict(goals[0]) if len(goals) == 1 else {}
            row = await self.db.execute_returning_row(
                """INSERT INTO aios.character_work_request(request_id,instance_id,source_timeline_id,source_node_id,intent,intent_hash,goal_id,attempt_id)
                   VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8) RETURNING *""",
                req.request_id,
                instance_id,
                source["source_timeline_id"],
                req.source_node_id,
                json.dumps(intent),
                digest,
                goal.get("goal_id"),
                goal.get("attempt_id"),
            )
            return await self.advance(row)

    async def alias(self, req, instance_id, digest, canonical):
        await self.db.execute(
            "INSERT INTO aios.work_request_alias(request_id,canonical_request_id,instance_id,source_node_id,intent_hash) VALUES($1,$2,$3,$4,$5) ON CONFLICT(request_id) DO NOTHING",
            req.request_id,
            canonical,
            instance_id,
            req.source_node_id,
            digest,
        )

    def receipt(self, row):
        return {
            "request_id": str(row["request_id"]),
            "status": row["status"],
            "reason": row["reason"],
            "run_id": str(row["run_id"]) if row["run_id"] else None,
            "completed": False,
        }

    async def state(self, row, status, reason, **extra):
        updated = await self.db.execute_returning_row(
            """UPDATE aios.character_work_request SET status=$2,reason=$3,updated_at=now(),
                profile_id=COALESCE($4,profile_id),profile_snapshot=COALESCE($5::jsonb,profile_snapshot),
                run_id=COALESCE($6,run_id) WHERE request_id=$1 RETURNING *""",
            row["request_id"],
            status,
            reason,
            extra.get("profile_id"),
            json.dumps(extra["profile_snapshot"])
            if "profile_snapshot" in extra
            else None,
            extra.get("run_id"),
        )
        return self.receipt(updated)

    async def advance(self, row, *, approved=False):
        from .environment_links import EnvironmentStore
        from .policy import ActionPolicyService
        from .workspaces import DelegationIn

        if row["run_id"] or row["status"] in {"denied", "invalidated"}:
            return self.receipt(row)
        intent = WorkIntent.model_validate(decode(row["intent"]))
        profiles = await self.db.fetch(
            """SELECT p.*,b.instance_id AS execution_instance_id,b.last_seen_at,
                      l.execution_binding_id,l.origin_source_timeline_id
               FROM aios.workspace_work_profile p JOIN aios.character_environment_link l USING(link_id)
               JOIN aios.workspace_binding b ON b.binding_id=l.execution_binding_id
               WHERE l.origin_instance_id=$1 AND l.enabled AND p.enabled AND p.capability=$2 AND p.purpose=$3
                 AND l.origin_source_timeline_id=$4
               ORDER BY b.last_seen_at DESC NULLS LAST,p.profile_id""",
            row["instance_id"],
            intent.capability,
            intent.purpose,
            row["source_timeline_id"],
        )
        if row["profile_id"]:
            profiles = [p for p in profiles if p["profile_id"] == row["profile_id"]]
        if not profiles:
            available = await self.db.fetch(
                """SELECT DISTINCT p.capability,p.purpose
                   FROM aios.workspace_work_profile p JOIN aios.character_environment_link l USING(link_id)
                   WHERE l.origin_instance_id=$1 AND l.origin_source_timeline_id=$2 AND l.enabled AND p.enabled
                   ORDER BY p.capability,p.purpose LIMIT 12""",
                row['instance_id'], row['source_timeline_id'])
            names = ', '.join(f"{p['capability']} / {p['purpose']}" for p in available)
            return await self.state(
                row,
                "needs_configuration",
                "No enabled authorized capability profile matches this purpose. "
                + (f"Available pairs: {names}. Copy both identifiers exactly; put the task in objective."
                   if names else "Ask the operator to enable a work profile."),
            )
        selected = None
        for profile in profiles:
            policy = ActionPolicyService(self.db)
            decisions = [
                await policy.disposition(owner, "workspace.run", "external_sensitive")
                for owner in (row["instance_id"], profile["execution_instance_id"])
            ]
            if "deny" not in decisions:
                selected = profile
                break
        if selected is None:
            return await self.state(
                row, "denied", "Workspace action policy denies this work"
            )
        p = selected
        # Lock the same binding/link/origin coordinate order as ordinary delegation.
        from .workspaces import WorkspaceStore

        await WorkspaceStore(self.db).binding(p["execution_binding_id"], lock=True)
        link = await EnvironmentStore(self.db).link(
            p["execution_binding_id"], p["link_id"]
        )
        current_profile = await self.db.fetchrow(
            "SELECT enabled FROM aios.workspace_work_profile WHERE profile_id=$1 FOR UPDATE",
            p["profile_id"],
        )
        if not current_profile or not current_profile["enabled"]:
            return await self.state(
                row, "needs_configuration", "Capability profile was disabled"
            )
        # Fence concurrent policy changes through the same transaction as queueing.
        await self.db.fetch(
            "SELECT instance_id FROM aios.character_action_policy WHERE instance_id IN ($1,$2) AND action_type='workspace.run' FOR SHARE",
            row["instance_id"],
            p["execution_instance_id"],
        )
        decisions = [
            await policy.disposition(owner, "workspace.run", "external_sensitive")
            for owner in (row["instance_id"], p["execution_instance_id"])
        ]
        if "deny" in decisions:
            return await self.state(
                row, "denied", "Workspace action policy denies this work"
            )
        try:
            await EnvironmentStore(self.db).origin(
                link, ancestor_node_id=row["source_node_id"]
            )
        except HTTPException:
            return await self.state(
                row,
                "invalidated",
                "Original source is no longer on the active conversation branch",
            )
        template = decode(p["assignment_template"])
        if (
            row["profile_snapshot"] is not None
            and decode(row["profile_snapshot"]) != template
        ):
            return await self.state(
                row, "invalidated", "Accepted profile criteria changed"
            )
        pinned = {"profile_id": p["profile_id"], "profile_snapshot": template}
        if "require_approval" in decisions and not (approved or row["approved_by"]):
            return await self.state(
                row,
                "awaiting_approval",
                "Work requires operator authorization",
                **pinned,
            )
        online = await self.db.fetchval(
            "SELECT last_seen_at>now()-interval '45 seconds' FROM aios.workspace_binding WHERE binding_id=$1",
            p["execution_binding_id"],
        )
        if not online:
            return await self.state(
                row,
                "waiting_client",
                "Authorized workspace worker has not polled recently",
                **pinned,
            )
        goal = {}
        if row["goal_id"]:
            open_attempt = await self.db.fetchval(
                """SELECT EXISTS(SELECT 1 FROM aios.character_goal_attempt a JOIN aios.character_agent_goal g USING(goal_id)
                   WHERE a.attempt_id=$1 AND a.goal_id=$2 AND a.instance_id=$3 AND g.instance_id=$3
                     AND g.status='active' AND a.closed_at IS NULL AND a.source_timeline_id=$4)""",
                row["attempt_id"],
                row["goal_id"],
                row["instance_id"],
                row["source_timeline_id"],
            )
            if not open_attempt:
                return await self.state(
                    row, "invalidated", "Original goal attempt is no longer open"
                )
            goal = {"goal_id": row["goal_id"], "attempt_id": row["attempt_id"]}
        objective = intent.objective
        if intent.constraints:
            objective += "\nAdditional requested constraints:\n" + "\n".join(
                "- " + c for c in intent.constraints
            )
        from .work_profiles import expand_request_template, EXPERIMENT_CAPABILITY, EXPERIMENT_PURPOSE, EXPERIMENT_GUIDANCE
        if (p['capability'], p['purpose']) == (EXPERIMENT_CAPABILITY, EXPERIMENT_PURPOSE):
            objective += EXPERIMENT_GUIDANCE
        try:
            spec = DelegationIn(
                request_id=row["request_id"],
                source_node_id=row["source_node_id"],
                objective=objective,
                **expand_request_template(template, row['request_id']),
                **goal,
            )
        except ValueError:
            return await self.state(
                row,
                "needs_configuration",
                "Request and profile exceed assignment limits; narrow the objective or criteria",
                **pinned,
            )
        # This internal path uses ancestry validation above, allowing later ordinary chat.
        run = await WorkspaceStore(self.db).submit(
            p["execution_binding_id"],
            spec,
            environment=link,
            operator_assignment=bool(approved or row["approved_by"]),
        )
        return await self.state(
            row,
            "queued",
            "Assignment admitted; awaiting worker claim",
            run_id=UUID(run["run_id"]),
            **pinned,
        )

    async def inspect(self, instance_id, request_id):
        from .workspaces import public

        row = await self.db.fetchrow(
            """SELECT r.*,p.capability,p.purpose,l.execution_binding_id,b.root_hint,b.worker_key
               FROM aios.character_work_request r LEFT JOIN aios.workspace_work_profile p USING(profile_id)
               LEFT JOIN aios.character_environment_link l ON l.link_id=p.link_id
               LEFT JOIN aios.workspace_binding b ON b.binding_id=l.execution_binding_id
               WHERE r.instance_id=$1 AND r.request_id=$2""",
            instance_id,
            request_id,
        )
        if not row:
            raise HTTPException(404, "Unknown work request")
        result = public(row)
        if row["run_id"]:
            execution = await self.db.fetchrow(
                """SELECT run_id,status,objective,assignment,result,cancel_requested,updated_at
                   FROM aios.workspace_run WHERE run_id=$1""", row["run_id"])
            result["execution"] = public(execution) if execution else None
        return result

    async def decide(self, instance_id, request_id, req):
        async with self.db.atomic():
            row = await self.db.fetchrow(
                "SELECT * FROM aios.character_work_request WHERE instance_id=$1 AND request_id=$2 FOR UPDATE",
                instance_id,
                request_id,
            )
            if not row:
                raise HTTPException(404, "Unknown work request")
            decision = req.model_dump()
            if row["decision"]:
                if decode(row["decision"]) == decision:
                    return self.receipt(row)
                raise HTTPException(
                    409, "Work request already has a different decision"
                )
            if row["run_id"] or row["status"] in {"denied", "invalidated"}:
                raise HTTPException(409, "Request already admitted or closed")
            if req.accept and (not row["profile_id"] or not row["profile_snapshot"]):
                raise HTTPException(
                    409, "Configure and pin execution criteria before approval"
                )
            await self.db.execute(
                "UPDATE aios.character_work_request SET decision=$2::jsonb,decision_reason=$3 WHERE request_id=$1",
                request_id,
                json.dumps(decision),
                req.reason,
            )
            if not req.accept:
                return await self.state(row, "denied", req.reason)
            await self.db.execute(
                "UPDATE aios.character_work_request SET approved_by=$2 WHERE request_id=$1",
                request_id,
                req.reviewer,
            )
            return await self.advance(row, approved=True)

    async def dismiss(self, instance_id, request_id, req):
        """Operator-only closure of unadmitted work; never cancels a run."""
        async with self.db.atomic():
            row = await self.db.fetchrow(
                "SELECT * FROM aios.character_work_request WHERE instance_id=$1 AND request_id=$2 FOR UPDATE",
                instance_id, request_id,
            )
            if not row:
                raise HTTPException(404, "Unknown work request")
            if row["run_id"]:
                raise HTTPException(409, "Request already admitted; use run cancellation")
            reason = f"Dismissed by {req.reviewer}: {req.reason}"
            if row["status"] in {"denied", "invalidated"}:
                if row["reason"] == reason:
                    return self.receipt(row)
                raise HTTPException(409, "Request already closed")
            # Preserve original intent, pinned criteria and any prior approval.
            return await self.state(row, "invalidated", reason)

    async def reconsider(self, *, instance_id=None, binding_id=None):
        async with self.db.atomic():
            rows = await self.db.fetch(
                """SELECT r.* FROM aios.character_work_request r WHERE r.run_id IS NULL
                   AND r.status IN ('requested','waiting_client','needs_configuration','awaiting_approval')
                   AND ($1::uuid IS NULL OR r.instance_id=$1)
                   AND ($2::uuid IS NULL OR EXISTS(SELECT 1 FROM aios.character_environment_link l
                         WHERE l.origin_instance_id=r.instance_id AND l.execution_binding_id=$2 AND l.enabled))
                   ORDER BY r.created_at LIMIT 5 FOR UPDATE OF r SKIP LOCKED""",
                instance_id,
                binding_id,
            )
            for row in rows:
                await self.advance(row)

    async def from_source(self, instance_id, node_id):
        # Check origin role/identity even before parsing; quoted human/tool text cannot act.
        async with self.db.atomic():
            source = await self.source(instance_id, node_id, character=True)
            old = await self.db.fetchval(
                "SELECT result FROM aios.work_source_receipt WHERE instance_id=$1 AND source_node_id=$2",
                instance_id,
                node_id,
            )
            if old:
                saved = decode(old)
                if saved.get("request_id"):
                    row = await self.db.fetchrow(
                        "SELECT * FROM aios.character_work_request WHERE request_id=$1",
                        UUID(saved["request_id"]),
                    )
                    return self.receipt(row)
                return saved
            try:
                intent = extract_work_intent(source["message_text"])
            except ValueError:
                result = {
                    "status": "needs_clarification",
                    "reason": "Invalid structured work request. Use one request_work action with capability, purpose, objective and optional constraints only.",
                    "completed": False,
                }
            else:
                if intent is None:
                    return None
                key = uuid5(NAMESPACE_URL, f"aios:work:{instance_id}:{node_id}")
                result = await self.request(
                    instance_id,
                    WorkRequestIn(
                        request_id=key, source_node_id=node_id, **intent.model_dump()
                    ),
                    character_source=True,
                )
            # Store source transport admission, not a transcript or model-generated fact.
            await self.db.execute(
                "INSERT INTO aios.work_source_receipt VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING",
                instance_id,
                node_id,
                json.dumps(result),
            )
            return result

    async def overlay(self, instance_id, timeline_id):
        from .workspace_registrations import RegistrationStore
        routing_problem = await RegistrationStore(self.db).ensure_available(instance_id)
        profiles = await self.db.fetch(
            """SELECT p.capability,p.purpose,p.description,
                       bool_or(b.last_seen_at>now()-interval '45 seconds') AS online
               FROM aios.workspace_work_profile p JOIN aios.character_environment_link l USING(link_id)
               JOIN aios.workspace_binding b ON b.binding_id=l.execution_binding_id
               JOIN aios.character_runtime_state es ON es.instance_id=b.instance_id
               JOIN aios.character_instance ei ON ei.instance_id=b.instance_id
               JOIN aios.character_instance oi ON oi.instance_id=l.origin_instance_id
               WHERE l.origin_instance_id=$1 AND l.origin_source_timeline_id=$2 AND l.enabled AND p.enabled
                 AND ((b.attached AND b.instance_id=l.origin_instance_id AND b.timeline_id=l.origin_source_timeline_id)
                      OR (NOT b.attached AND b.instance_id<>l.origin_instance_id AND b.timeline_id<>l.origin_source_timeline_id
                          AND COALESCE(ei.current_world_id,ei.world_id)<>COALESCE(oi.current_world_id,oi.world_id)))
                 AND NOT EXISTS(SELECT 1 FROM aios.workspace_registration reg
                      WHERE reg.registration_id=b.registration_id AND (NOT reg.enabled OR reg.disposition='deny'))
                 AND es.source_timeline_id=b.timeline_id
                 AND (b.attached OR es.source_head_node_id IS NOT DISTINCT FROM b.last_node_id)
                 AND NOT EXISTS(SELECT 1 FROM aios.character_action_policy ap
                      WHERE ap.instance_id IN (l.origin_instance_id,b.instance_id) AND ap.action_type='workspace.run' AND ap.disposition='deny')
               GROUP BY p.capability,p.purpose,p.description ORDER BY p.capability,p.purpose LIMIT 12""",
            instance_id,
            timeline_id,
        )
        rows = await self.db.fetch(
            """SELECT r.request_id,r.status,r.reason,r.intent,r.run_id,w.status AS execution_status
               FROM aios.character_work_request r LEFT JOIN aios.workspace_run w USING(run_id)
                   WHERE r.instance_id=$1 AND r.source_timeline_id=$2
                   AND r.status NOT IN ('denied','invalidated')
                   ORDER BY (COALESCE(w.status,r.status) IN
                     ('requested','waiting_client','needs_configuration','awaiting_approval','queued','running')) DESC,
                     r.created_at DESC LIMIT 8""",
            instance_id,
            timeline_id,
        )
        history_count = await self.db.fetchval(
            """SELECT count(*) FROM aios.character_work_request
               WHERE instance_id=$1 AND source_timeline_id=$2
                 AND status IN ('denied','invalidated')""", instance_id, timeline_id)
        error = await self.db.fetchval(
            """SELECT s.result FROM aios.work_source_receipt s JOIN aios.character_runtime_state rs ON rs.instance_id=s.instance_id
               WHERE s.instance_id=$1 AND s.source_node_id=rs.source_head_node_id
                 AND rs.source_timeline_id=$2 AND s.result->>'status'='needs_clarification'""",
            instance_id,
            timeline_id,
        )
        unavailable = None
        if not profiles:
            links = await self.db.fetch(
                """SELECT l.enabled,l.origin_source_timeline_id,
                          EXISTS(SELECT 1 FROM aios.workspace_work_profile p
                                 WHERE p.link_id=l.link_id AND p.enabled) AS has_profile
                   FROM aios.character_environment_link l WHERE l.origin_instance_id=$1""",
                instance_id)
            current = [l for l in links if l["enabled"] and l["origin_source_timeline_id"] == timeline_id]
            if not links:
                unavailable = "No execution environment is linked. Ask the operator to complete Workspace Delegation setup."
            elif not current:
                unavailable = "No enabled environment link matches this conversation source. Operator pairing needs reconciliation."
            elif not any(l["has_profile"] for l in current):
                unavailable = "No enabled work profile is configured for this conversation."
            else:
                unavailable = "No eligible capability: instance pairing or workspace.run policy blocks execution. Ask the operator to check delegation readiness."
        return {
            "unavailable_reason": routing_problem or unavailable,
            "request_error": decode(error) if error else None,
            "history_count": history_count,
            "capabilities": [dict(p) for p in profiles],
            "requests": [
                {
                    **self.receipt(r),
                    "status": r["execution_status"] or r["status"],
                    "reason": "See REMOTE WORK for execution evidence and progress"
                    if r["execution_status"]
                    else r["reason"],
                    "objective": decode(r["intent"])["objective"][:200],
                }
                for r in rows
            ],
            "tool": tool_schema() if profiles else None,
        }


async def ingest_work_request(db, instance_ids, node_id):
    """Ingestion owns source adoption; failures here must not poison its receipt."""
    for instance_id in instance_ids:
        try:
            await WorkAdmission(db).from_source(instance_id, node_id)
        except (ValueError, HTTPException) as exc:
            logging.getLogger(__name__).info(
                "Work request rejected instance=%s source=%s reason=%s",
                instance_id,
                node_id,
                type(exc).__name__,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Work admission failed instance=%s source=%s", instance_id, node_id
            )


def register_work_action(db, registry):
    from .actions import ActionSpec

    async def request(instance_id, arguments):
        source = await db.fetchval(
            "SELECT source_head_node_id FROM aios.character_runtime_state WHERE instance_id=$1",
            instance_id,
        )
        if source is None:
            raise ValueError("Work requests require an active source")
        intent = WorkIntent.model_validate(
            {k: v for k, v in arguments.items() if not k.startswith("_")}
        )
        return await WorkAdmission(db).request(
            instance_id,
            WorkRequestIn(
                request_id=uuid5(
                    NAMESPACE_URL,
                    f"aios:work-action:{instance_id}:{source}:{fingerprint(intent.model_dump())}",
                ),
                source_node_id=source,
                **intent.model_dump(),
            ),
        )

    registry.register(
        ActionSpec(
            name="work.request",
            schema=tool_schema()["function"]["parameters"],
            side_effect_class="internal_write",
            allowed_worker_classes=frozenset({"executive", "planning"}),
            handler=request,
            result_mode="final",
            capability_class="workspace",
            description="Propose purpose-based work to AIOS admission. A receipt is not execution or goal completion.",
        )
    )
