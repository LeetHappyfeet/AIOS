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
        meta: Mapping[str, Any] | None = None, attempt_id: UUID | None = None,
    ) -> bool:
        await self.record_goal_evidence(
            instance_id=instance_id,goal_id=goal_id,evidence_type=evidence_type,
            relation="completion_candidate",evidence_id=evidence_id,
            source_node_id=source_node_id,confidence=confidence,meta=meta,
        )
        try:
            await self.goals.finish(
                instance_id=instance_id, goal_id=goal_id, status="completed",
                resolution_kind=resolution_kind, verification="reviewed",
                source_node_id=source_node_id, evidence_ids=[evidence_id] if evidence_id else [], attempt_id=attempt_id
            )
        except LookupError:
            await self.reconcile_goal_threads(instance_id=instance_id,goal_id=goal_id)
            return False
        await self.reconcile_goal_threads(instance_id=instance_id,goal_id=goal_id)
        return True

    async def reconcile_goal_threads(self, *, instance_id: UUID, goal_id: UUID) -> None:
        goal=await self.db.fetchrow(
            "SELECT status,meta FROM aios.character_agent_goal WHERE goal_id=$1 AND instance_id=$2",
            goal_id,instance_id)
        if not goal:
            return
        status=str(goal["status"])
        if status not in {"completed","failed","cancelled","dormant"}:
            return
        meta=self._mapping(goal["meta"])
        reason=str(meta.get("resolution_kind") or {"cancelled":"cancelled","failed":"goal_failed","completed":"goal_completed","dormant":"dormant"}[status])
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

        attempt_id = operation.get("goal_attempt_id")
        if goal_id is not None and "goal_attempt_id" in operation:
            current = await self.db.fetchrow(
                "SELECT attempt_id FROM aios.character_goal_attempt WHERE goal_id=$1 AND instance_id=$2 AND closed_at IS NULL",
                goal_id, operation["instance_id"])
            if not attempt_id or not current or current["attempt_id"] != attempt_id:
                return  # Stale work remains telemetry, not new-attempt evidence.

        if goal_id is not None and terminal_status == "succeeded":
            operation_type=str(operation.get("operation_type") or "")
            if operation_type == "planning.review" and result.get("kind") == "choice":
                # A successful review is not itself progress. The selected
                # semantic outcome is the only source of goal evidence.
                option_index=int(result.get("option_index",-1))
                relation_by_option={
                    0: None,                    # unresolved/open
                    1: "progress",
                    2: "completion_candidate",
                    3: "contradiction",          # opportunity passed unsatisfied
                    4: "withdrawal",
                }
                relation=relation_by_option.get(option_index)
                if option_index == 2:
                    # A review is not proof that its originating message has
                    # already fulfilled a new intention. Only a live attempt
                    # with a later source node on the same timeline can close.
                    review_source = operation.get("source_node_id")
                    review_valid = False
                    if attempt_id and review_source:
                        review = await self.db.fetchrow(
                            """SELECT (
                                  g.status='active' AND a.closed_at IS NULL
                                  AND (a.source_timeline_id IS NULL
                                       OR current_node.timeline_id=a.source_timeline_id)
                                  AND (origin.node_id IS NULL
                                       OR (origin.timeline_id=current_node.timeline_id
                                           AND current_node.event_id>origin.event_id))
                                 ) AS eligible
                               FROM aios.character_goal_attempt a
                               JOIN aios.character_agent_goal g ON g.goal_id=a.goal_id
                                  AND g.instance_id=a.instance_id
                               JOIN aios.dag_node current_node ON current_node.node_id=$4
                               LEFT JOIN aios.dag_node origin ON origin.node_id=g.source_node_id
                               WHERE a.goal_id=$1 AND a.instance_id=$2
                                 AND a.attempt_id=$3""",
                            goal_id, operation["instance_id"], attempt_id, review_source,
                        )
                        review_valid = bool(review and review["eligible"])
                    if review_valid:
                        await self.complete_goal(
                            instance_id=operation["instance_id"],goal_id=goal_id,
                            resolution_kind="reviewed_satisfied",
                            evidence_type="bounded_goal_review",
                            evidence_id=str(operation["operation_id"]),
                            source_node_id=review_source,confidence=.8,
                            meta={"label":result.get("label"),"option_index":option_index,
                                  "source_after_goal":True},
                            attempt_id=attempt_id,
                        )
                elif relation is not None:
                    lifecycle_effect=(
                        "opportunity_missed" if option_index==3
                        else "dormant" if option_index==4 else None)
                    await self.record_goal_evidence(
                        instance_id=operation["instance_id"],goal_id=goal_id,
                        evidence_type="bounded_goal_review",relation=relation,
                        evidence_id=str(operation["operation_id"]),
                        source_node_id=operation.get("source_node_id"),
                        confidence=.8 if option_index==3 else (.7 if option_index==1 else .65),
                        meta={"label":result.get("label"),"option_index":option_index,
                              **({"lifecycle_effect":lifecycle_effect} if lifecycle_effect else {})},
                    )
                    if option_index in {3,4}:
                        goal_status="failed" if option_index==3 else "dormant"
                        try:
                            await self.goals.finish(
                                instance_id=operation["instance_id"],
                                goal_id=goal_id,status=goal_status,
                                resolution_kind=lifecycle_effect, verification="reviewed",
                                source_node_id=operation.get("source_node_id"),
                                evidence_ids=[str(operation["operation_id"])], attempt_id=attempt_id)
                        except LookupError:
                            pass
            elif operation_type != "planning.review":
                # Successful non-review work can be evidence of progress.
                await self.record_goal_evidence(
                    instance_id=operation["instance_id"],goal_id=goal_id,
                    evidence_type="cognitive_operation",relation="progress",
                    evidence_id=str(operation["operation_id"]),
                    source_node_id=operation.get("source_node_id"),confidence=.65,
                    meta={"operation_type":operation_type,
                          "status":terminal_status,"result":dict(result)},
                )
        # Failed/stale machinery is operational telemetry, not evidence that
        # the character's goal is blocked. It remains on the operation/thread
        # receipt below without contaminating character_goal_evidence.

        if not thread_id:
            return
        thread=await self.db.fetchrow(
            """SELECT t.goal_id,g.status AS goal_status
               FROM aios.character_cognitive_thread t
               LEFT JOIN aios.character_agent_goal g ON g.goal_id=t.goal_id
               WHERE t.thread_id=$1""",thread_id)
        if thread and thread["goal_id"] and str(thread["goal_status"]) in {"completed","failed","cancelled","dormant"}:
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
