from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.hud.frame import HUDAssembler
from aios_app.hud.render_text import render_hud_text
from aios_app.inference import InferenceBroker, InferenceRequest
from .actions import ActionDispatcher, default_action_registry
from .lifecycle import CharacterAgencyStore
from .runtime import AgentRuntimeStore
from .deterministic import DEFAULT_DETERMINISTIC_TASKS, DeterministicTaskRegistry


PROFILE_BY_WORKER = {
    "executive": "agent.executive",
    "research": "agent.research",
    "planning": "agent.planning",
    "reflection": "agent.reflection",
    "communication": "agent.communication",
}


class CharacterWorker:
    def __init__(self, db: Database, deterministic: DeterministicTaskRegistry | None = None):
        self.db = db
        self.agency = CharacterAgencyStore(db)
        self.runtime = AgentRuntimeStore(db)
        self.registry = default_action_registry(db)
        self.dispatcher = ActionDispatcher(db, self.registry)
        self.broker = InferenceBroker(db)
        self.hud = HUDAssembler(db)
        self.deterministic = deterministic or DEFAULT_DETERMINISTIC_TASKS

    async def _first_person_prompt(
        self, *, instance_id: UUID, worker_class: str, objective: str,
        profile_name: str,
    ) -> tuple[str, int]:
        row = await self.db.fetchrow(
            """
            SELECT ci.character_id, COALESCE(ci2.display_name, ci2.canonical_name, ci.character_id) AS name,
                   rs.state_version
            FROM aios.character_instance ci
            JOIN aios.character_identity ci2 ON ci2.character_id=ci.character_id
            JOIN aios.character_runtime_state rs ON rs.instance_id=ci.instance_id
            WHERE ci.instance_id=$1
            """,
            instance_id,
        )
        if not row:
            raise LookupError(f"Unknown active character instance {instance_id}")
        frame = await self.hud.build(
            instance_id, profile_name=profile_name, focus_text=objective
        )
        hud_text = render_hud_text(frame)
        schemas = self.registry.schemas_for(worker_class)
        prompt = (
            f"I am {row['name']}.\n"
            "The following AIOS HUD is my authoritative current cognitive context. "
            "I must reason and speak in first person as this character. "
            "Task specialization limits what I may do; it does not change who I am.\n\n"
            f"{hud_text}\n\n"
            f"CURRENT {worker_class.upper()} OBJECTIVE:\n{objective}\n\n"
            "AVAILABLE ACTIONS (names and argument schemas):\n"
            f"{json.dumps(schemas, default=str)}\n\n"
            "Choose what I should express and zero or more bounded actions."
        )
        return prompt, int(row["state_version"])

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

        profile = task.hud_profile_name or PROFILE_BY_WORKER.get(
            worker_class, "agent.executive"
        )
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
            await self.runtime.assert_inference_budget(task.instance_id, task.task_id)
            prompt, state_version = await self._first_person_prompt(
                instance_id=task.instance_id, worker_class=worker_class,
                objective=task.retrieval_focus or task.objective,
                profile_name=profile,
            )
            schemas = self.registry.schemas_for(worker_class)
            inference = await self.broker.infer(InferenceRequest(
                instance_id=task.instance_id, task_id=task.task_id,
                worker_class=worker_class, prompt=prompt,
                context_state_version=state_version, hud_profile_name=profile,
                allowed_actions=schemas,
            ))
            await self.db.execute(
                """
                UPDATE aios.character_agent_runtime
                SET last_inference_at=now(), state='acting', updated_at=now()
                WHERE instance_id=$1
                """,
                task.instance_id,
            )

            action_results = []
            await self.runtime.assert_action_budget(task.instance_id, task.task_id, len(inference.response.actions))
            for index, proposal in enumerate(inference.response.actions):
                spec = self.registry.get(proposal.type)
                if not spec:
                    continue
                idem = hashlib.sha256(
                    f"{inference.request_id}:{index}:{proposal.type}".encode()
                ).hexdigest()
                action = await self.agency.create_action(
                    instance_id=task.instance_id, task_id=task.task_id,
                    action_type=proposal.type, arguments=proposal.arguments,
                    side_effect_class=spec.side_effect_class,
                    idempotency_key=idem, expected_state_version=state_version,
                    proposed_by=f"inference:{inference.request_id}",
                )
                action = await self.dispatcher.dispatch(
                    action.action_id, worker_class=worker_class
                )
                action_results.append({
                    "action_id": str(action.action_id), "type": action.action_type,
                    "status": action.status, "result": action.result,
                    "error": action.error, "rejection_reason": action.rejection_reason,
                })
                await self.runtime.wake(
                    instance_id=task.instance_id,
                    event_type=(
                        "ACTION_COMPLETED" if action.status == "succeeded"
                        else "ACTION_FAILED"
                    ),
                    source_type="action", source_id=str(action.action_id),
                    payload={"task_id": str(task.task_id), "status": action.status},
                    dedupe_key=f"action-terminal:{action.action_id}:{action.status}",
                )

            result = {
                "expression": inference.response.expression,
                "inference_request_id": str(inference.request_id),
                "provider": inference.provider_key,
                "model": inference.model,
                "actions": action_results,
            }
            await self.agency.transition_task(task_id, "succeeded", result=result)
            await self.runtime.finish_semantic_turn(task.instance_id, semantic=True)
            return result
        except Exception as exc:
            await self.agency.transition_task(task_id, "failed", error=str(exc)[:2000])
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
