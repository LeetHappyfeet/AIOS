from __future__ import annotations

from typing import Any
from uuid import UUID

from aios_app.db import Database
from .lifecycle import CharacterAgencyStore
from .runtime import AgentRuntimeStore
from .deterministic import DEFAULT_DETERMINISTIC_TASKS, DeterministicTaskRegistry
from .cognitive_delta import CognitiveDeltaService
from .admission import AutonomyAdmissionService


class CharacterWorker:
    def __init__(self, db: Database, deterministic: DeterministicTaskRegistry | None = None):
        self.db = db
        self.agency = CharacterAgencyStore(db)
        self.runtime = AgentRuntimeStore(db)
        self.deterministic = deterministic or DEFAULT_DETERMINISTIC_TASKS
        self.deltas = CognitiveDeltaService(db)
        self.admission = AutonomyAdmissionService(db)

    async def _notify_parent(
        self, task, status: str, result: dict[str, Any],
    ) -> None:
        if task.parent_task_id is None:
            return
        parent = await self.agency.get_task(task.parent_task_id)
        if parent is None:
            return
        await self.runtime.wake(
            instance_id=task.instance_id,
            event_type="COGNITIVE_TASK_COMPLETED" if status == "succeeded" else "COGNITIVE_TASK_FAILED",
            source_type="cognitive_task", source_id=str(task.task_id),
            payload={
                "child_task_id": str(task.task_id),
                "parent_task_id": str(parent.task_id),
                "task_type": task.task_type,
                "status": status,
                "result": result,
                "source_node_id": str(task.source_node_id or ""),
                "root_task_id": str(task.root_task_id or parent.root_task_id or parent.task_id),
            },
            priority=parent.priority,
            dedupe_key=f"cognitive-child:{task.task_id}:{status}",
        )

    async def run_task(self, task_id: UUID) -> dict[str, Any]:
        task = await self.agency.get_task(task_id)
        if not task:
            raise LookupError(f"Unknown cognitive task {task_id}")
        if task.status == "queued":
            task = await self.agency.transition_task(task_id, "running")
        elif task.status != "running":
            raise ValueError(f"Task {task_id} is not runnable from {task.status}")

        worker_class = task.task_type
        deterministic_spec = self.deterministic.get(worker_class)
        use_deterministic = (
            task.execution_mode == "deterministic"
            or (task.execution_mode == "auto" and deterministic_spec is not None)
        )
        if task.execution_mode == "deterministic" and deterministic_spec is None:
            await self.agency.transition_task(
                task_id, "failed",
                error=f"No deterministic implementation registered for {worker_class}",
            )
            raise ValueError(
                f"No deterministic implementation registered for {worker_class}"
            )
        if use_deterministic and deterministic_spec is not None:
            await self.runtime.ensure(task.instance_id)
            await self.db.execute(
                """
                UPDATE aios.character_agent_runtime
                SET state='acting', active_task_id=$2,
                    last_activity_at=now(), updated_at=now()
                WHERE instance_id=$1
                """,
                task.instance_id, task.task_id,
            )
            try:
                result = dict(await deterministic_spec.handler(
                    task.instance_id,
                    {
                        "objective": task.objective,
                        "retrieval_focus": task.retrieval_focus,
                        "task_id": str(task.task_id),
                    },
                ))
                result["execution_mode"] = "deterministic"
                await self.agency.transition_task(task_id, "succeeded", result=result)
                await self.runtime.finish_semantic_turn(task.instance_id, semantic=False)
                return result
            except Exception as exc:
                await self.agency.transition_task(task_id, "failed", error=str(exc)[:2000])
                await self.runtime.finish_semantic_turn(task.instance_id, semantic=False)
                raise

        await self.runtime.ensure(task.instance_id)
        await self.db.execute(
            """
            UPDATE aios.character_agent_runtime
            SET state='thinking', active_task_id=$2, last_activity_at=now(), updated_at=now()
            WHERE instance_id=$1
            """,
            task.instance_id, task.task_id,
        )
        try:
            # General agent cognition no longer exposes action schemas to the
            # model. AIOS constructs executable opportunities and the model may
            # only select an opaque choice key in InternalCognitionTransactions.
            from .opportunity_router import OpportunityRouter
            tx_id = await OpportunityRouter(self.db).admit(
                instance_id=task.instance_id,
                source_node_id=task.source_through_node_id or task.source_node_id,
                source_task_id=task.task_id,
                enqueue_inference=False,
            )
            if tx_id is None:
                result = {
                    "execution_mode": "bounded_choice",
                    "status": "no_prepared_operation",
                    "expression": "",
                    "actions": [],
                }
                await self.agency.transition_task(task_id, "succeeded", result=result)
                await self._notify_parent(task, "succeeded", result)
                await self.admission.mark_episode_succeeded(
                    instance_id=task.instance_id,
                    through_node_id=task.source_through_node_id,
                )
                await self.runtime.finish_semantic_turn(task.instance_id, semantic=True)
                return result

            from .transactions import InternalCognitionTransactions
            decision = await InternalCognitionTransactions(self.db).run(tx_id)
            result = {
                "execution_mode": "bounded_choice",
                "transaction_id": str(tx_id),
                "choice": decision.choice,
                "operation": decision.operation,
                "focus": decision.focus,
                "decision_status": decision.status,
                "expression": "",
                "actions": [],
            }
            if decision.status != "consumed":
                raise RuntimeError(
                    f"bounded cognitive decision ended as {decision.status}"
                )
            await self.agency.transition_task(task_id, "succeeded", result=result)
            await self._notify_parent(task, "succeeded", result)
            await self.runtime.finish_semantic_turn(task.instance_id, semantic=True)
            return result
        except Exception as exc:
            await self.agency.transition_task(task_id, "failed", error=str(exc)[:2000])
            await self._notify_parent(task, "failed", {"error": str(exc)[:2000]})
            await self.db.execute(
                """
                UPDATE aios.character_agent_runtime
                SET state='ready', active_task_id=NULL, active_action_id=NULL,
                    last_activity_at=now(), updated_at=now()
                WHERE instance_id=$1
                """,
                task.instance_id,
            )
            raise
