"""Project-scoped, private, append-only notebooks (Pass A).

No embeddings, LLM calls, or automatic HUD injection on write/read.
All public access is checked against the current instance, timeline and goal.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .work_admission import extract_work_action

log = logging.getLogger(__name__)
ENTRY_TYPES = ("note", "observation", "question", "decision", "next_step", "worker_report")


class NoteAppend(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal_ref: UUID
    entry_type: Literal["note", "observation", "question", "decision", "next_step", "worker_report"] = "note"
    content: str = Field(min_length=1, max_length=4000)

    @classmethod
    def from_action(cls, raw: dict) -> "NoteAppend":
        from aios_app.agent.work_admission import expand_reference
        if raw.get("op") != "notebook.append":
            raise ValueError("not a notebook append action")
        data = {k: v for k, v in raw.items() if k != "op"}
        if "goal_ref" in data:
            data["goal_ref"] = expand_reference(data["goal_ref"], "g")
        return cls.model_validate(data)


class NoteQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal_ref: UUID
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=30)
    max_chars: int = Field(default=6000, ge=200, le=12000)
    before_id: UUID | None = None


class NotebookStore:
    def __init__(self, db):
        self.db = db

    async def _scope(self, instance_id: UUID, goal_id: UUID, *, require_active: bool = True):
        """Resolve scope from trusted SQL, never user-supplied instance/timeline."""
        row = await self.db.fetchrow(
            """SELECT rs.source_timeline_id, g.status, a.attempt_id
               FROM aios.character_runtime_state rs
               JOIN aios.character_agent_goal g ON g.instance_id=rs.instance_id
               LEFT JOIN LATERAL (
                 SELECT attempt_id FROM aios.character_goal_attempt a
                 WHERE a.instance_id=g.instance_id AND a.goal_id=g.goal_id
                   AND a.source_timeline_id=rs.source_timeline_id
                   AND a.closed_at IS NULL
                 ORDER BY a.started_at DESC LIMIT 1
               ) a ON true
               WHERE rs.instance_id=$1 AND g.goal_id=$2""",
            instance_id, goal_id,
        )
        if not row or not row["source_timeline_id"]:
            raise HTTPException(404, "Notebook project not found on the active timeline")
        if require_active and (row["status"] != "active" or row["attempt_id"] is None):
            raise HTTPException(409, "Notebook writes require an active goal attempt")
        return row

    async def append(self, instance_id: UUID, req: NoteAppend, *,
                     origin: str, source_node_id: UUID | None = None,
                     work_request_id: UUID | None = None) -> dict:
        text = req.content.strip()
        if not text:
            raise HTTPException(422, "Note text cannot be blank")
        if origin not in ("character", "worker"):
            raise HTTPException(403, "Invalid notebook author")
        if origin == "worker" and work_request_id is None:
            raise HTTPException(403, "Worker notes require a work request")
        async with self.db.atomic():
            scope = await self._scope(instance_id, req.goal_ref)
            timeline = scope["source_timeline_id"]
            if origin == "character":
                if source_node_id is None:
                    raise HTTPException(403, "Character notes need an adopted source node")
                source = await self.db.fetchrow(
                    """SELECT n.node_id FROM aios.dag_node n
                       JOIN aios.ingest_event e ON e.event_id=n.event_id
                       WHERE n.node_id=$1 AND n.timeline_id=$2
                         AND n.speaker_role='character' AND e.superseded_at IS NULL""",
                    source_node_id, timeline,
                )
                if not source:
                    raise HTTPException(403, "Source node is not a current character message")
            if origin == "worker":
                await self._work_scope(instance_id, work_request_id, req.goal_ref, timeline, scope["attempt_id"])
            notebook_id = await self.db.fetchval(
                """INSERT INTO aios.character_notebook(instance_id,source_timeline_id,goal_id)
                   VALUES($1,$2,$3)
                   ON CONFLICT(instance_id,source_timeline_id,goal_id)
                   DO UPDATE SET goal_id=EXCLUDED.goal_id
                   RETURNING notebook_id""",
                instance_id, timeline, req.goal_ref,
            )
            receipt_key = (f"node:{source_node_id}" if origin == "character"
                           else f"work:{work_request_id}:" +
                                hashlib.sha256((req.entry_type + "\0" + text).encode()).hexdigest())
            # A source node can write at most one note; replays are idempotent.
            row = await self.db.fetchrow(
                """INSERT INTO aios.character_notebook_entry(
                     notebook_id,instance_id,source_timeline_id,goal_id,author_kind,
                     entry_type,content,source_node_id,work_request_id,receipt_key)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT(notebook_id,receipt_key) DO NOTHING
                   RETURNING entry_id,created_at""",
                notebook_id, instance_id, timeline, req.goal_ref, origin,
                req.entry_type, text, source_node_id, work_request_id, receipt_key,
            )
            if row is None:
                row = await self.db.fetchrow(
                    """SELECT entry_id,created_at FROM aios.character_notebook_entry
                       WHERE notebook_id=$1 AND receipt_key=$2""",
                    notebook_id, receipt_key,
                )
                # Never silently accept changed contents under the same source node.
                if origin == "character":
                    previous = await self.db.fetchval(
                        """SELECT content FROM aios.character_notebook_entry
                           WHERE notebook_id=$1 AND receipt_key=$2""",
                        notebook_id, receipt_key,
                    )
                    if previous != text:
                        raise HTTPException(409, "Source note already exists with different contents")
        return {"status": "stored", "notebook_id": str(notebook_id),
                "note_ref": str(row["entry_id"]), "goal_ref": str(req.goal_ref),
                "created_at": row["created_at"].isoformat()}

    async def _work_scope(self, instance_id, request_id, goal_id, timeline, attempt_id):
        allowed = await self.db.fetchval(
            """SELECT 1 FROM aios.character_work_request r
               JOIN aios.workspace_run w ON w.run_id=r.run_id
               WHERE r.instance_id=$1 AND r.request_id=$2 AND r.goal_id=$3
                 AND r.source_timeline_id=$4 AND r.attempt_id=$5 AND w.status='running'
                 AND r.status NOT IN ('denied','invalidated','dismissed')
               LIMIT 1""",
            instance_id, request_id, goal_id, timeline, attempt_id,
        )
        if not allowed:
            raise HTTPException(403, "No running authorized project work for this notebook")

    async def list(self, instance_id: UUID, req: NoteQuery) -> dict:
        scope = await self._scope(instance_id, req.goal_ref, require_active=False)
        if req.query is not None and not req.query.strip():
            raise HTTPException(422, "Search query cannot be empty")
        rows = await self.db.fetch(
            """SELECT e.entry_id,e.entry_type,e.content,e.author_kind,
                      e.source_node_id,e.work_request_id,e.created_at
               FROM aios.character_notebook_entry e
               WHERE e.instance_id=$1 AND e.source_timeline_id=$2 AND e.goal_id=$3
                 AND ($4::text IS NULL OR e.search_vector @@ plainto_tsquery('english',$4))
                 AND ($5::uuid IS NULL OR (e.created_at,e.entry_id) <
                     (SELECT c.created_at,c.entry_id FROM aios.character_notebook_entry c
                      WHERE c.entry_id=$5 AND c.instance_id=$1
                        AND c.source_timeline_id=$2 AND c.goal_id=$3))
               ORDER BY e.created_at DESC,e.entry_id DESC
               LIMIT $6""",
            instance_id, scope["source_timeline_id"], req.goal_ref,
            req.query, req.before_id, req.limit,
        )
        notes = []
        remaining = req.max_chars
        for r in rows:
            if remaining <= 0:
                break
            excerpt = r["content"][:remaining]
            remaining -= len(excerpt)
            notes.append({
                "note_ref": str(r["entry_id"]), "type": r["entry_type"],
                "content": excerpt, "truncated": len(excerpt) < len(r["content"]),
                "author": r["author_kind"],
                "source_node_id": str(r["source_node_id"]) if r["source_node_id"] else None,
                "work_request_id": str(r["work_request_id"]) if r["work_request_id"] else None,
                "created_at": r["created_at"].isoformat(),
            })
            if len(excerpt) < len(r["content"]):
                break
        return {
            "goal_ref": str(req.goal_ref),
            "notes": notes,
            "next_before_id": str(notes[-1]["note_ref"]) if notes and len(rows) >= len(notes) else None,
        }


async def ingest_notebook_action(db, instance_ids, node_id):
    """Best-effort post-ingest admission; a bad note never poisons chat ingestion."""
    for instance_id in instance_ids:
        try:
            source = await db.fetchrow(
                "SELECT message_text FROM aios.dag_node WHERE node_id=$1", node_id,
            )
            action = extract_work_action(source["message_text"] if source else "")
            if action and action.get("op") == "notebook.append":
                await NotebookStore(db).append(
                    instance_id, NoteAppend.from_action(action),
                    origin="character", source_node_id=node_id,
                )
        except Exception:
            log.exception("Notebook action rejected: instance=%s node=%s", instance_id, node_id)

class WorkerNotebookAppend(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    work_request_id: UUID
    note: NoteAppend


class WorkerNotebookRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    work_request_id: UUID
    query: NoteQuery


def install_notebook_routes(app, db):
    """Native client interface; only the running linked work request can use it."""
    import hmac
    import os
    from fastapi import APIRouter, Depends, Header

    async def authorize(authorization: str = Header(default="")):
        from aios_app.agent.workspaces import get_workspace_api_key
        key = os.environ.get("AIOS_WORK_REQUEST_API_KEY", "")
        if not key or key in (os.environ.get("AIOS_WORKSPACE_REVIEW_KEY"), get_workspace_api_key()):
            raise HTTPException(503, "Configure a distinct AIOS_WORK_REQUEST_API_KEY")
        if not hmac.compare_digest(authorization, "Bearer " + key):
            raise HTTPException(401, "Invalid notebook API key")

    router = APIRouter(prefix="/agent/instance", dependencies=[Depends(authorize)])
    store = NotebookStore(db)

    @router.post("/{instance_id}/notebook/append")
    async def append_note(instance_id: UUID, req: WorkerNotebookAppend):
        return await store.append(instance_id, req.note, origin="worker",
                                  work_request_id=req.work_request_id)

    @router.post("/{instance_id}/notebook/read")
    async def read_notes(instance_id: UUID, req: WorkerNotebookRead):
        scope = await store._scope(instance_id, req.query.goal_ref)
        await store._work_scope(instance_id, req.work_request_id,
                                req.query.goal_ref, scope["source_timeline_id"], scope["attempt_id"])
        return await store.list(instance_id, req.query)

    app.include_router(router)
