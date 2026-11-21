"""Notebook tools for an AIOS Client run, using its existing workspace lease."""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .notebooks import NoteAppend, NoteQuery, NoteType, NotebookStore
from .workspace_contract import Assignment

CAPABILITY = "workspace-notebook-v1"


class NotebookLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_key: str
    claim_token: UUID


class NotebookRecent(NotebookLease):
    before_id: UUID | None = None
    limit: int = Field(default=4, ge=1, le=8, strict=True)


class NotebookSearch(NotebookLease):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=4, ge=1, le=8, strict=True)


class NotebookAppend(NotebookLease):
    entry_type: NoteType = "note"
    content: str = Field(min_length=1, max_length=4000)


class WorkspaceNotebooks:
    def __init__(self, workspace):
        self.workspace = workspace
        self.db = workspace.db
        self.notes = NotebookStore(self.db)

    async def scope(self, binding_id, run_id, req, *, write=False):
        """Resolve and check both execution authority and the originating project."""
        binding, run = await self.workspace.owned(binding_id, run_id, req)
        await self.workspace.current(binding)
        instance_id = run["origin_instance_id"]
        timeline_id = run["origin_source_timeline_id"]
        if (run["required_protocol_version"] != 3 or run["environment_link_id"] is None
                or instance_id is None or timeline_id is None):
            raise HTTPException(403, "Notebook tools require a goal-linked delegated run")
        raw_spec = run["assignment"]
        spec = (Assignment.model_validate_json(raw_spec) if isinstance(raw_spec, str)
                else Assignment.model_validate(raw_spec))
        if spec.goal_id is None:
            raise HTTPException(403, "Notebook tools require a goal-linked delegated run")
        if write and spec.inspection_only():
            raise HTTPException(403, "Inspection-only assignments cannot append project notes")

        await self.workspace.open_goal({"instance_id": instance_id, "timeline_id": timeline_id}, spec)
        scope = await self.notes._scope(instance_id, spec.goal_id)
        if (scope["source_timeline_id"] != timeline_id or scope["attempt_id"] != spec.attempt_id):
            raise HTTPException(409, "The notebook goal attempt or timeline changed")
        await self.notes._work_scope(instance_id, run["request_id"], spec.goal_id,
                                     timeline_id, spec.attempt_id, run_id=run_id)
        return instance_id, timeline_id, spec.goal_id, run["request_id"]

    async def recent(self, binding_id, run_id, req, *, brief=False):
        async with self.db.atomic():
            instance_id, timeline_id, goal_id, _ = await self.scope(binding_id, run_id, req)
            result = await self.notes.list(instance_id, NoteQuery(
                goal_ref=goal_id, before_id=None if brief else req.before_id,
                limit=3 if brief else req.limit, max_chars=1200 if brief else 3000,
            ), max_note_chars=400 if brief else None)
            result["status"] = "retrieved"
            result["epistemic_warning"] = "Attributed working notes; use source receipts for evidence."
            if brief:
                result["summary"] = await self.summary(instance_id, timeline_id, goal_id)
            return result

    async def summary(self, instance_id, timeline_id, goal_id):
        from .notebook_consolidation import NotebookConsolidator

        notebook_id = await self.db.fetchval(
            """SELECT notebook_id FROM aios.character_notebook
               WHERE instance_id=$1 AND source_timeline_id=$2 AND goal_id=$3""",
            instance_id, timeline_id, goal_id,
        )
        if notebook_id:
            row = await NotebookConsolidator(self.db).latest(instance_id, notebook_id)
            if row["status"] == "curated":
                summary = row["summary"]
                return {
                    "revision": row["revision"],
                    "covered_count": row["covered_count"],
                    "summary": summary["summary"][:500],
                    "open_questions": summary["open_questions"][:2],
                    "next_steps": summary["next_steps"][:2],
                }
        return None

    async def search(self, binding_id, run_id, req):
        from .notebook_recall import ProjectNoteRecall

        if not req.query.strip():
            raise HTTPException(422, "Notebook search requires a nonblank query")
        async with self.db.atomic():
            scope = await self.scope(binding_id, run_id, req)
            instance_id, timeline_id, goal_id, _ = scope
        # Optional vector I/O runs without holding the lease locks. Recheck
        # authority before releasing the result to the caller.
        result = await ProjectNoteRecall(self.db).retrieve(
            instance_id, goal_id, timeline_id, req.query, limit=req.limit, max_chars=2400,
        )
        async with self.db.atomic():
            if await self.scope(binding_id, run_id, req) != scope:
                raise HTTPException(409, "The notebook project changed during retrieval")
        return result

    async def append(self, binding_id, run_id, req):
        async with self.db.atomic():
            instance_id, _, goal_id, request_id = await self.scope(binding_id, run_id, req, write=True)
            return await self.notes.append(
                instance_id, NoteAppend(goal_ref=goal_id, entry_type=req.entry_type, content=req.content),
                origin="worker", work_request_id=request_id,
            )
