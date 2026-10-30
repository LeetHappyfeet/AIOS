"""Operator setup and read-only diagnostics for chat delegation."""
from __future__ import annotations

from uuid import UUID, uuid4
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .environment_links import EnvironmentStore
from .policy import ActionPolicyService


class ExecutionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_instance_id: UUID
    label: str = Field(default="Execution workspace", min_length=1, max_length=160)


class WorkspaceSetup:
    def __init__(self, db):
        self.db = db

    async def characters(self):
        return [dict(r) for r in await self.db.fetch(
            "SELECT character_id,display_name FROM aios.character_identity ORDER BY character_id")]

    async def origin(self, instance_id):
        row = await self.db.fetchrow(
            """SELECT ci.character_id,rs.instance_id,COALESCE(ci.current_world_id,ci.world_id) AS world_id,rs.source_timeline_id,
                      rs.source_head_node_id,t.session_id,t.user_name,t.scope_key
               FROM aios.character_runtime_state rs JOIN aios.character_instance ci USING(instance_id)
               JOIN aios.timeline t ON t.timeline_id=rs.timeline_id WHERE rs.instance_id=$1""", instance_id)
        if not row or not row['session_id']:
            raise HTTPException(409, "Select a session-backed conversational instance")
        return row

    async def create_execution(self, req):
        from aios_app.dag import get_or_create_timeline
        from aios_app.ingest_api import ingest_message
        from aios_app.models import IngestIn
        from aios_app.world.runtime import WorldRuntimeService
        from .workspaces import public

        async with self.db.atomic():
            origin = await self.origin(req.origin_instance_id)
            session = await self.db.fetchval(
                """INSERT INTO aios.session(source,source_session_id,topic,meta)
                   VALUES('workspace-client',$1,$2,'{}'::jsonb) RETURNING session_id""",
                str(uuid4()), req.label)
            # Create an independent source, then record the actual operator setup
            # event so the client's first context request has a durable anchor.
            source = await get_or_create_timeline(
                self.db, world_key="liminal", session_id=session,
                character_id=origin['character_id'], user_name=origin['user_name'],
                scope_key=origin['scope_key'], meta={"source": "workspace-client"})
            result = await WorldRuntimeService(self.db).activate_character(
                character_id=origin['character_id'], user_name=origin['user_name'],
                session_id=session, scope_key=origin['scope_key'])
            if result.instance_id == req.origin_instance_id or result.world_id == origin['world_id']:
                raise HTTPException(409, "Execution requires an independent world")
            await self.db.execute(
                "UPDATE aios.character_runtime_state SET source_timeline_id=$2 WHERE instance_id=$1",
                result.instance_id, source)
            receipt = await ingest_message(self.db, IngestIn(
                session_id=session, character_id=origin['character_id'],
                user_name=origin['user_name'], scope_key=origin['scope_key'],
                speaker_type="tool", speaker_id="workspace-setup", recipient_id=origin['character_id'],
                kind="tool_result", text="Operator created this separate execution environment. No workspace task has run.",
                payload={"source": "aios-workspace-setup", "execution_instance_id": str(result.instance_id)},
                dedupe_key=f"workspace-setup:{result.instance_id}"))
            if receipt.timeline_id != source or not receipt.source_current:
                raise HTTPException(409, "Setup observation did not anchor the execution source")
            return public(await self.origin(result.instance_id))

    async def bindings(self, character_id):
        from .workspaces import public
        return [public(r) for r in await self.db.fetch(
            "SELECT * FROM aios.workspace_binding WHERE character_id=$1 ORDER BY binding_id", character_id)]

    async def readiness(self, instance_id, binding_id=None):
        from .workspaces import WorkspaceStore, public
        origin = await self.origin(instance_id)
        links = await self.db.fetch(
            """SELECT l.*,b.last_seen_at>now()-interval '45 seconds' AS online
               FROM aios.character_environment_link l
               JOIN aios.workspace_binding b ON b.binding_id=l.execution_binding_id
               WHERE l.origin_instance_id=$1 AND ($2::uuid IS NULL OR l.execution_binding_id=$2)
               ORDER BY l.link_id""", instance_id, binding_id)
        environments = []
        for link in links:
            problems = []
            policies = {}
            try:
                async with self.db.atomic():
                    store = WorkspaceStore(self.db)
                    b = await store.binding(link['execution_binding_id'], lock=True)
                    await store.current(b)
                    if b['last_node_id'] is None:
                        problems.append("Execution source needs an initial setup observation before the worker can fetch context")
                    await EnvironmentStore(self.db).link(b['binding_id'], link['link_id'])
                    await EnvironmentStore(self.db).origin(link)
                    execution = await self.origin(b['instance_id'])
                    if (b['instance_id'] == instance_id
                        or execution['world_id'] == origin['world_id']
                        or b['timeline_id'] == origin['source_timeline_id']):
                        raise HTTPException(409, "Execution must have a separate instance, world and source")
            except HTTPException as exc:
                problems.append(str(exc.detail))
            profiles = await self.db.fetch(
                "SELECT * FROM aios.workspace_work_profile WHERE link_id=$1 ORDER BY capability,purpose", link['link_id'])
            if not any(p['enabled'] for p in profiles):
                problems.append("No enabled work profile; configure capability, purpose and host checks")
            for role, owner in (("origin", instance_id), ("execution", link['execution_instance_id'])):
                policies[role] = await ActionPolicyService(self.db).disposition(owner, "workspace.run", "external_sensitive")
            if 'deny' in policies.values():
                problems.append("workspace.run is denied by origin or execution policy")
            configured = not problems
            if not link['online']:
                problems.append("Worker has not polled in the last 45 seconds; start the v3 listener")
            environments.append({"link_id": str(link['link_id']),
                "binding_id": str(link['execution_binding_id']), "enabled": link['enabled'],
                "share_memory": link['share_memory'], "worker_online": bool(link['online']),
                "configured": configured, "ready": not problems,
                "approval_required": 'require_approval' in policies.values(),
                "policies": policies, "profiles": [public(p) for p in profiles], "problems": problems})
        problems = []
        if not origin['source_timeline_id'] or not origin['source_head_node_id']:
            problems.append("Conversational instance needs a source observation before requesting work")
        if not links:
            problems.append("No environment link; connect an execution binding to this conversation and source timeline")
            if binding_id:
                b = await WorkspaceStore(self.db).binding(binding_id)
                if b['instance_id'] == instance_id:
                    problems.append("Client is bound to the conversational instance; create a separate execution instance")
                if b['character_id'] != origin['character_id'] or b['user_name'] != origin['user_name']:
                    problems.append("Execution binding must use the same character and human user")
        return {"origin": public(origin), "ready": not problems and any(e['ready'] for e in environments),
                "configured": not problems and any(e['configured'] for e in environments),
                "problems": problems, "environments": environments,
                "note": "Authentication and binding success do not establish chat delegation readiness. Approval may still be required."}
