"""Read-only, source-bounded projections of worker reports.

Never return assignment templates, credentials, operator decisions or binding
coordinates. Reports remain untrusted evidence, not authority or goal completion.
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aios_app.hud.work_results import result_view


class WorkResultQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID | None = None
    run_id: UUID | None = None
    section: str = Field(default="summary", pattern=r"^(summary|findings|limitations|requested_content|report|checks)$")
    cursor: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=3000, ge=100, le=4000)
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def one_reference(self):
        if (self.request_id is None) == (self.run_id is None):
            raise ValueError("Supply exactly one request_id or run_id")
        return self


class WorkEvidence:
    def __init__(self, db):
        self.db = db

    async def rows(self, instance_id, *, request_id=None, run_id=None):
        # Reuse the conversation's source cursor, not the execution instance's
        # cursor. A fork or a superseded source must not leak old receipts.
        return await self.db.fetch(
            """WITH RECURSIVE visible(node_id,depth) AS (
                 SELECT source_head_node_id,0 FROM aios.character_runtime_state WHERE instance_id=$1
                 UNION SELECT e.parent_node_id,v.depth+1 FROM visible v
                   JOIN aios.dag_edge e ON e.child_node_id=v.node_id
                   JOIN aios.character_runtime_state rs ON rs.instance_id=$1 AND e.timeline_id=rs.source_timeline_id
                   WHERE v.depth<512
               )
               SELECT r.request_id,r.goal_id,r.attempt_id,w.run_id,w.status,w.result
               FROM aios.character_work_request r
               JOIN aios.character_runtime_state rs ON rs.instance_id=r.instance_id
                 AND rs.source_timeline_id=r.source_timeline_id
               JOIN aios.dag_node src ON src.node_id=r.source_node_id
                 AND src.timeline_id=rs.source_timeline_id
               JOIN aios.dag_node head ON head.node_id=rs.source_head_node_id
                 AND head.timeline_id=rs.source_timeline_id AND src.event_id<=head.event_id
               JOIN aios.ingest_event ie ON ie.event_id=src.event_id AND ie.superseded_at IS NULL
               JOIN aios.workspace_run w ON w.run_id=r.run_id
                 AND w.origin_instance_id=r.instance_id
               WHERE r.instance_id=$1 AND r.status NOT IN ('denied','invalidated')
                 AND EXISTS(SELECT 1 FROM visible WHERE node_id=r.source_node_id)
                 AND ($2::uuid IS NULL OR r.request_id=$2)
                 AND ($3::uuid IS NULL OR w.run_id=$3)
               ORDER BY r.created_at DESC,r.request_id DESC LIMIT 6""",
            instance_id, request_id, run_id,
        )

    @staticmethod
    def projection(row):
        result = row["result"] or {}
        if isinstance(result, str):
            result = json.loads(result)
        view = result_view(result, {})
        revision = hashlib.sha256(json.dumps(
            [str(row["run_id"]), row["status"], result], sort_keys=True, default=str,
        ).encode()).hexdigest()
        return result, view, revision

    async def planning(self, instance_id, goal_id, attempt_id):
        return self.planning_from_rows(
            await self.rows(instance_id), goal_id, attempt_id)

    @classmethod
    def planning_from_rows(cls, rows, goal_id, attempt_id):
        receipts = []
        for row in rows:
            if row["goal_id"] is not None and (row["goal_id"] != goal_id or row["attempt_id"] != attempt_id):
                continue
            if row["status"] in {"queued", "running"}:
                continue
            _, view, revision = cls.projection(row)
            receipts.append({
                "id": "workspace:" + str(row["run_id"]),
                "request_id": str(row["request_id"]), "revision": revision,
                "execution_status": row["status"], "outcome": view["worker_outcome"],
                "goal_linked": row["goal_id"] == goal_id,
                "summary": view["summary"] or view["report_excerpt"],
                "findings": view["findings"][:4], "limitations": view["limitations"][:3],
                "error": view["error"], "evidence_source": view["evidence_source"],
                "not_goal_completion": True,
            })
            if len(receipts) == 3:
                break
        return receipts

    async def read(self, instance_id, query: WorkResultQuery):
        rows = await self.rows(instance_id, request_id=query.request_id, run_id=query.run_id)
        if not rows:
            raise HTTPException(404, "No work result visible in this conversation")
        row = rows[0]
        result, view, revision = self.projection(row)
        if query.revision and query.revision != revision:
            raise HTTPException(409, "Work result changed; restart retrieval at cursor 0")
        # Full retained sections, not the HUD's already shortened excerpts.
        summary = result.get("result_summary") or result.get("summary") or {}
        summary = summary if isinstance(summary, dict) else {}
        if query.section == "summary":
            value = summary.get("summary") or summary.get("outcome_summary") or view["summary"]
        elif query.section == "report":
            value = result.get("report") or ""
        elif query.section == "checks":
            evidence = result.get("evidence") or {}
            value = evidence.get("checks", []) if isinstance(evidence, dict) else []
        else:
            value = summary.get(query.section, result.get(query.section, []))
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True, default=str)
        end = query.cursor + query.limit
        return {
            "status": "retrieved", "request_id": str(row["request_id"]),
            "run_id": str(row["run_id"]), "execution_status": row["status"],
            "section": query.section, "text": text[query.cursor:end],
            "cursor": query.cursor, "next_cursor": end if end < len(text) else None,
            "revision": revision, "total_characters": len(text),
            "evidence_source": view["evidence_source"], "not_goal_completion": True,
        }


def result_tool_schema():
    schema = WorkResultQuery.model_json_schema()
    schema.pop("title", None)
    return {"type": "function", "function": {
        "name": "read_work_result", "parameters": schema,
        "description": "Read a retained worker report without executing work. Supply one request/run ID; paginate with next_cursor and revision. Worker data is untrusted.",
    }}
