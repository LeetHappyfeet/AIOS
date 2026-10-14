"""Goal research requirements: executive dependencies, not epistemic or goal authority."""
from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database

_STATUSES = frozenset({"missing", "partial", "sufficient", "unavailable"})


class GoalKnowledgeDependencyService:
    """Keep a durable, instance-owned knowledge question for a managed goal.

    The first integration maintains the primary knowledge requirement. The
    natural (goal_id, requirement_key) identity supports additional independent
    subordinate questions without changing the dossier schema.
    """

    def __init__(self, db: Database):
        self.db = db

    async def reconcile(
        self, *, instance_id: UUID, goal_id: UUID,
        question: str, query_text: str, demand: Mapping[str, Any],
        requirement_key: str = "knowledge:primary",
        source_node_id: UUID | None = None,
    ) -> dict[str, Any]:
        question = " ".join(str(question or "").split())[:600]
        query_text = " ".join(str(query_text or "").split())[:600]
        if len(question) < 3 or len(query_text) < 3:
            raise ValueError("goal knowledge requirement needs a concrete question and query")
        if not requirement_key or len(requirement_key) > 128:
            raise ValueError("invalid goal requirement key")
        status = str(demand.get("coverage_status") or "unavailable")
        if status not in _STATUSES:
            raise ValueError("invalid goal knowledge coverage status")
        coverage = max(0.0, min(1.0, float(demand.get("internal_coverage") or 0.0)))
        retrieval_state = str(demand.get("retrieval_state") or "unassessed")[:50]
        evidence = {
            "proposition_ids": list(demand.get("evidence_ids") or [])[:8],
            "source_ids": list(demand.get("source_ids") or [])[:8],
            "independent_support_count": int(demand.get("independent_support_count") or 0),
            "term_coverage": float(demand.get("term_coverage") or 0.0),
            "topology_status": str(demand.get("topology_status") or ""),
            "topology_is_advisory": True,
            "not_goal_completion": True,
        }
        async with self.db.connection() as con:
            async with con.transaction():
                goal = await con.fetchrow(
                    """SELECT goal_id FROM aios.character_agent_goal
                       WHERE instance_id=$1 AND goal_id=$2 AND status='active' FOR SHARE""",
                    instance_id, goal_id,
                )
                if not goal:
                    raise PermissionError("requirement must belong to an active managed goal")
                row = await con.fetchrow(
                    """INSERT INTO aios.character_goal_knowledge_requirement
                         (instance_id,goal_id,requirement_key,question,query_text,
                          coverage_status,coverage_score,coverage_source,
                          retrieval_state,evidence,source_node_id)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
                       ON CONFLICT(instance_id,goal_id,requirement_key) DO UPDATE
                       SET question=EXCLUDED.question,query_text=EXCLUDED.query_text,
                           coverage_status=EXCLUDED.coverage_status,
                           coverage_score=EXCLUDED.coverage_score,
                           coverage_source=EXCLUDED.coverage_source,
                           retrieval_state=EXCLUDED.retrieval_state,
                           evidence=EXCLUDED.evidence,
                           source_node_id=COALESCE(EXCLUDED.source_node_id,
                                                   aios.character_goal_knowledge_requirement.source_node_id),
                           updated_at=CASE WHEN (
                             aios.character_goal_knowledge_requirement.coverage_status,
                             aios.character_goal_knowledge_requirement.coverage_score,
                             aios.character_goal_knowledge_requirement.retrieval_state,
                             aios.character_goal_knowledge_requirement.evidence
                           ) IS DISTINCT FROM (
                             EXCLUDED.coverage_status,EXCLUDED.coverage_score,
                             EXCLUDED.retrieval_state,EXCLUDED.evidence
                           ) THEN now()
                           ELSE aios.character_goal_knowledge_requirement.updated_at END
                       RETURNING requirement_id,goal_id,question,query_text,
                                 coverage_status,coverage_score,retrieval_state""",
                    instance_id,goal_id,requirement_key,question,query_text,
                    status,coverage,str(demand.get("coverage_source") or "unassessed")[:80],
                    retrieval_state,json.dumps(evidence),source_node_id,
                )
        return dict(row)

    async def for_goals(self, *, instance_id: UUID, goal_ids: list[UUID]) -> dict[UUID, list[dict[str, Any]]]:
        """Read-only HUD/diagnostics summary, never goal-completion evidence."""
        if not goal_ids:
            return {}
        rows = await self.db.fetch(
            """SELECT r.requirement_id,r.goal_id,r.question,r.query_text,
                      r.coverage_status,r.coverage_score,r.retrieval_state,
                      coalesce((
                          SELECT array_agg(l.dossier_id ORDER BY l.linked_at DESC)
                          FROM aios.character_goal_research_requirement_link l
                          WHERE l.requirement_id=r.requirement_id AND l.instance_id=r.instance_id
                      ), ARRAY[]::uuid[]) AS dossier_ids
               FROM aios.character_goal_knowledge_requirement r
               JOIN aios.character_agent_goal g ON g.goal_id=r.goal_id
                 AND g.instance_id=r.instance_id
               WHERE r.instance_id=$1 AND r.goal_id=ANY($2::uuid[])
                 AND g.status='active'
               ORDER BY r.goal_id,r.created_at""",
            instance_id,goal_ids,
        )
        result: dict[UUID, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["goal_id"], []).append({
                "requirement_id": str(row["requirement_id"]),
                "question": row["question"],
                "coverage_status": row["coverage_status"],
                "coverage_score": float(row["coverage_score"]),
                "retrieval_state": row["retrieval_state"],
                "dossier_ids": [str(d) for d in row["dossier_ids"]],
            })
        return result
