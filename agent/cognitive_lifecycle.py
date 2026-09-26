from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService


class CognitiveLifecycleReconciler:
    """Turn execution/evidence receipts into goal and thread lifecycle state.

    Execution success is evidence, never completion by itself. Only explicit
    terminal goal state or a validated completion candidate may resolve a
    goal-linked cognitive thread.
    """

    def __init__(self, db: Database):
        self.db = db
        self.goals = CharacterGoalService(db)

    async def record_goal_evidence(
        self, *, instance_id: UUID, goal_id: UUID, evidence_type: str,
        relation: str, evidence_id: str | None = None,
        source_node_id: UUID | None = None, confidence: float = 0.5,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        if relation not in {"progress","completion_candidate","blocker","contradiction","withdrawal"}:
            raise ValueError(f"invalid goal evidence relation {relation!r}")
        await self.db.execute(
            """INSERT INTO aios.character_goal_evidence(
                   goal_id,instance_id,evidence_type,relation,evidence_id,
                   source_node_id,confidence,meta)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
               ON CONFLICT DO NOTHING""",
            goal_id, instance_id, evidence_type, relation, evidence_id,
            source_node_id, max(0.0,min(1.0,float(confidence))),
            json.dumps(dict(meta or {}),default=str),
        )

    async def complete_goal(
        self, *, instance_id: UUID, goal_id: UUID, resolution_kind: str,
        evidence_type: str, evidence_id: str | None = None,
        source_node_id: UUID | None = None, confidence: float = 1.0,
        meta: Mapping[str, Any] | None = None,
    ) -> bool:
        await self.record_goal_evidence(
            instance_id=instance_id,goal_id=goal_id,evidence_type=evidence_type,
            relation="completion_candidate",evidence_id=evidence_id,
            source_node_id=source_node_id,confidence=confidence,meta=meta,
        )
        row=await self.db.execute_returning_row(
            """UPDATE aios.character_agent_goal
               SET status='completed',completed_at=now(),updated_at=now(),
                   meta=meta || $3::jsonb
               WHERE goal_id=$1 AND instance_id=$2 AND status='active'
               RETURNING goal_id""",
            goal_id,instance_id,
            json.dumps({"resolution_kind":resolution_kind,
                        "completion_confidence":max(0.0,min(1.0,float(confidence)))},
                       default=str),
        )
        await self.reconcile_goal_threads(instance_id=instance_id,goal_id=goal_id)
        return bool(row)

    async def reconcile_goal_threads(self, *, instance_id: UUID, goal_id: UUID) -> None:
        goal=await self.db.fetchrow(
            "SELECT status,meta FROM aios.character_agent_goal WHERE goal_id=$1 AND instance_id=$2",
            goal_id,instance_id)
        if not goal:
            return
        status=str(goal["status"])
        if status not in {"completed","cancelled"}:
            return
        meta=self._mapping(goal["meta"])
        reason=str(meta.get("resolution_kind") or ("cancelled" if status=="cancelled" else "goal_completed"))
        await self.db.execute(
            """UPDATE aios.character_cognitive_thread
               SET status='resolved',resolved_at=COALESCE(resolved_at,now()),
                   pressure=0,
                   meta=meta || $3::jsonb,updated_at=now()
               WHERE instance_id=$1 AND goal_id=$2 AND status<>'resolved'""",
            instance_id,goal_id,
            json.dumps({"resolution_kind":reason,"resolved_by":"goal_lifecycle"},default=str),
        )

    async def reconcile_operation(
        self, *, operation: Mapping[str,Any], result: Mapping[str,Any],
        terminal_status: str = "succeeded",
    ) -> None:
        thread_id=operation.get("thread_id")
        payload=self._mapping(operation.get("input"))
        goal_id=self._uuid(payload.get("goal_id"))
        if goal_id is None and thread_id:
            row=await self.db.fetchrow(
                "SELECT goal_id FROM aios.character_cognitive_thread WHERE thread_id=$1",thread_id)
            goal_id=row["goal_id"] if row else None

        if goal_id is not None:
            relation="progress" if terminal_status=="succeeded" else "blocker"
            confidence=.65 if terminal_status=="succeeded" else .8
            await self.record_goal_evidence(
                instance_id=operation["instance_id"],goal_id=goal_id,
                evidence_type="cognitive_operation",
                relation=relation,evidence_id=str(operation["operation_id"]),
                source_node_id=operation.get("source_node_id"),confidence=confidence,
                meta={"operation_type":operation.get("operation_type"),
                      "status":terminal_status,"result":dict(result)},
            )
            # The bounded goal-review classifier's C choice is the only
            # inference outcome allowed to propose semantic completion.
            if (terminal_status=="succeeded"
                and str(operation.get("operation_type"))=="planning.review"
                and result.get("kind")=="choice"
                and int(result.get("option_index",-1))==2):
                await self.complete_goal(
                    instance_id=operation["instance_id"],goal_id=goal_id,
                    resolution_kind="reviewed_satisfied",
                    evidence_type="bounded_goal_review",
                    evidence_id=str(operation["operation_id"]),
                    source_node_id=operation.get("source_node_id"),confidence=.8,
                    meta={"label":result.get("label")},
                )

        if not thread_id:
            return
        thread=await self.db.fetchrow(
            """SELECT t.goal_id,g.status AS goal_status
               FROM aios.character_cognitive_thread t
               LEFT JOIN aios.character_agent_goal g ON g.goal_id=t.goal_id
               WHERE t.thread_id=$1""",thread_id)
        if thread and thread["goal_id"] and str(thread["goal_status"]) in {"completed","cancelled"}:
            await self.reconcile_goal_threads(
                instance_id=operation["instance_id"],goal_id=thread["goal_id"])
            return
        await self.db.execute(
            """UPDATE aios.character_cognitive_thread
               SET status='open',resolved_at=NULL,
                   pressure=CASE WHEN $2='succeeded' THEN GREATEST(0,pressure-1) ELSE pressure END,
                   meta=meta || $3::jsonb,updated_at=now()
               WHERE thread_id=$1""",
            thread_id,terminal_status,
            json.dumps({"last_operation":{"status":terminal_status,"result":dict(result)}},
                       default=str),
        )

    @staticmethod
    def _mapping(value: Any) -> dict[str,Any]:
        if isinstance(value,Mapping):
            return dict(value)
        if isinstance(value,str):
            try:
                decoded=json.loads(value)
            except (TypeError,ValueError,json.JSONDecodeError):
                return {}
            return dict(decoded) if isinstance(decoded,Mapping) else {}
        return {}

    @staticmethod
    def _uuid(value: Any) -> UUID | None:
        try:
            return UUID(str(value)) if value else None
        except (TypeError,ValueError,AttributeError):
            return None
