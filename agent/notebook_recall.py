"""Demand-driven project notebook retrieval.

Hybrid PostgreSQL full-text and optional Qdrant semantic candidates are always
re-checked against the instance's current source timeline and managed goal.
No retrieval occurs on an ordinary chat turn.
"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import HTTPException

from .notebook_vectors import semantic_candidates


class ProjectNoteRecall:
    def __init__(self, db):
        self.db = db

    async def retrieve(self, instance_id: UUID, goal_id: UUID, timeline_id: UUID,
                       query: str, *, limit: int = 4, max_chars: int = 1600,
                       semantic: bool = True) -> dict:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("Notebook query must contain 1-200 characters")
        limit = max(1, min(int(limit), 8))
        max_chars = max(200, min(int(max_chars), 4000))
        notebook = await self.db.fetchrow(
            """SELECT n.notebook_id FROM aios.character_notebook n
               JOIN aios.character_runtime_state rs ON rs.instance_id=n.instance_id
               JOIN aios.character_agent_goal g ON g.instance_id=n.instance_id
                    AND g.goal_id=n.goal_id
               WHERE n.instance_id=$1 AND n.source_timeline_id=$2
                 AND n.goal_id=$3 AND rs.source_timeline_id=$2
                 AND g.status='active'""", instance_id, timeline_id, goal_id,
        )
        if not notebook:
            return {"status": "no_project_notebook", "notes": [], "summary": None}
        notebook_id = notebook["notebook_id"]
        # Bounded lexical retrieval works when Qdrant is absent or behind.
        lexical = await self.db.fetch(
            """SELECT entry_id,content,entry_type,author_kind,created_at
               FROM aios.character_notebook_entry
               WHERE notebook_id=$1 AND instance_id=$2 AND source_timeline_id=$3
                 AND goal_id=$4
                 AND search_vector @@ plainto_tsquery('english',$5)
               ORDER BY ts_rank_cd(search_vector,plainto_tsquery('english',$5)) DESC,
                        created_at DESC LIMIT $6""",
            notebook_id, instance_id, timeline_id, goal_id, query, limit,
        )
        rows = [dict(r) for r in lexical]
        if semantic and len(rows) < limit and await self.db.fetchval(
            """SELECT 1 FROM aios.character_notebook_vector_state
               WHERE notebook_id=$1 LIMIT 1""", notebook_id,
        ):
            more = await semantic_candidates(
                self.db, instance_id=instance_id, timeline_id=timeline_id,
                goal_id=goal_id, notebook_id=notebook_id, query=query, limit=limit)
            used = {r["entry_id"] for r in rows}
            rows += [r for r in more if r["entry_id"] not in used][:limit - len(rows)]
        notes = []
        budget = max_chars
        for r in rows[:limit]:
            if budget <= 0:
                break
            excerpt = r["content"][:budget]
            budget -= len(excerpt)
            notes.append({
                "note_ref": str(r["entry_id"]), "type": r["entry_type"],
                "author": r["author_kind"], "excerpt": excerpt,
                "truncated": len(excerpt) < len(r["content"]),
            })
        from .notebook_consolidation import NotebookConsolidator
        curated = await NotebookConsolidator(self.db).latest(instance_id, notebook_id)
        summary = None
        if curated.get("status") == "curated":
            item = curated["summary"]
            # A summary is attributed working material, never ground truth.
            summary = {
                "revision": curated["revision"],
                "summary": str(item.get("summary") or "")[:500],
                "open_questions": item.get("open_questions", [])[:3],
                "next_steps": item.get("next_steps", [])[:3],
            }
        return {"status": "retrieved", "scope": "private_project",
                "notebook_id": str(notebook_id), "summary": summary, "notes": notes,
                "epistemic_warning": "Working notes and derived summaries; not verified facts or completed work"}
