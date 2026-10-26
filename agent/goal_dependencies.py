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
            "rdf_refs": list(demand.get("rdf_refs") or [])[:3],
            "recall_receipt": demand.get("recall_receipt") or {},
            "independent_support_count": int(demand.get("independent_support_count") or 0),
            "term_coverage": float(demand.get("term_coverage") or 0.0),
            "topology_status": str(demand.get("topology_status") or ""),
            "topology_is_advisory": True,
            "not_goal_completion": True,
            "dependency_kind": demand.get("dependency_kind"),
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
                           evidence=EXCLUDED.evidence || jsonb_build_object(
                               'rdf_refs',CASE WHEN $12 THEN EXCLUDED.evidence->'rdf_refs'
                                   ELSE COALESCE(aios.character_goal_knowledge_requirement.evidence->'rdf_refs','[]'::jsonb) END,
                               'recall_receipt',CASE WHEN $12 THEN EXCLUDED.evidence->'recall_receipt'
                                   ELSE COALESCE(aios.character_goal_knowledge_requirement.evidence->'recall_receipt','{}'::jsonb) END),
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
                    retrieval_state,json.dumps(evidence),source_node_id,"rdf_refs" in demand,
                )
        return dict(row)

    async def for_goals(self, *, instance_id: UUID, goal_ids: list[UUID]) -> dict[UUID, list[dict[str, Any]]]:
        """Read-only HUD/diagnostics summary, never goal-completion evidence."""
        if not goal_ids:
            return {}
        rows = await self.db.fetch(
            """SELECT r.requirement_id,r.goal_id,r.requirement_key,r.question,r.query_text,
                      r.coverage_status,r.coverage_score,r.retrieval_state,r.evidence,
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
               ORDER BY r.goal_id,r.created_at,r.requirement_key""",
            instance_id,goal_ids,
        )
        result: dict[UUID, list[dict[str, Any]]] = {}
        for row in rows:
            evidence = row["evidence"]
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
            evidence = evidence if isinstance(evidence, dict) else {}
            result.setdefault(row["goal_id"], []).append({
                "requirement_id": str(row["requirement_id"]),
                "requirement_key": row["requirement_key"],
                "question": row["question"],
                "coverage_status": row["coverage_status"],
                "coverage_score": float(row["coverage_score"]),
                "retrieval_state": row["retrieval_state"],
                "evidence_ids": list(evidence.get("proposition_ids") or [])[:8],
                "rdf_refs": list(evidence.get("rdf_refs") or [])[:3],
                "recall_receipt": evidence.get("recall_receipt") or {},
                "dependency_kind": evidence.get("dependency_kind"),
                "dossier_ids": [str(d) for d in row["dossier_ids"]],
            })
        return result

    async def record_inquiry(self, *, instance_id, goal_id, requirement_id, receipt, revision=None):
        """Attach references from a host-owned inquiry; never mark sufficient/progress."""
        data = receipt.get("result") or {}
        hits = data.get("hits") or []
        refs = [h.get("provenance", {}).get("rdf_ref") for h in hits
                if h.get("provenance", {}).get("rdf_ref")][:3]
        propositions = [h["evidence_id"] for h in hits if h.get("source")=="char"][:8]
        delta = {"rdf_refs": refs, "proposition_ids": propositions,
                 "recall_receipt": {"inquiry_id": receipt.get("inquiry_id"),
                    "status": receipt.get("status"), "reason": data.get("reason"), "revision":revision,
                    "evidence_ids": [h.get("evidence_id") for h in hits][:3]}}
        await self.db.execute("""UPDATE aios.character_goal_knowledge_requirement r
            SET evidence=r.evidence || $4::jsonb,
                coverage_status=CASE WHEN jsonb_array_length($4::jsonb->'rdf_refs')>0
                                     THEN 'partial' ELSE r.coverage_status END,
                retrieval_state='internal_recall',updated_at=now()
            WHERE r.instance_id=$1 AND r.goal_id=$2 AND r.requirement_id=$3
              AND EXISTS(SELECT 1 FROM aios.character_agent_goal g
                  WHERE g.goal_id=r.goal_id AND g.instance_id=r.instance_id AND g.status='active')""",
            instance_id,goal_id,requirement_id,json.dumps(delta))
