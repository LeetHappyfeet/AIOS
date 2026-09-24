from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.pipeline.jobs import enqueue_job
from .lifecycle import CharacterAgencyStore
from .runtime import AgentRuntimeStore


class AutonomyScheduler:
    """Cheap wake triage.

    Wake events do not directly invoke inference. They are coalesced into one
    cognitive task/job per instance. Heartbeats with no pending work remain
    deterministic and cost no LLM call.
    """

    def __init__(self, db: Database):
        self.db = db
        self.runtime = AgentRuntimeStore(db)
        self.agency = CharacterAgencyStore(db)

    async def _enqueue_existing_tasks(self, limit: int) -> int:
        rows = await self.db.fetch(
            """SELECT t.task_id,t.instance_id,t.priority
               FROM aios.character_cognitive_task t
               WHERE t.status='queued'
                 AND t.trigger_type IN ('api_task','cognitive_delegation','cognitive_resume','transaction_choice')
                 AND NOT EXISTS (
                   SELECT 1 FROM aios.pipeline_job j
                   WHERE j.job_type='agent_wake'
                     AND j.status IN ('queued','running')
                     AND j.payload->>'task_id'=t.task_id::text
                 )
               ORDER BY t.priority,t.created_at LIMIT $1""",
            max(1,min(int(limit),1000)),
        )
        count=0
        for row in rows:
            job_id=await enqueue_job(
                self.db,job_type="agent_wake",
                payload={"instance_id":str(row["instance_id"]),"task_id":str(row["task_id"])},
                priority=int(row["priority"]),
            )
            if job_id is not None: count += 1
        return count

    async def schedule_ready(self, limit: int = 100) -> int:
        direct = await self._enqueue_existing_tasks(limit)
        rows = await self.db.fetch(
            """
            SELECT ar.instance_id
            FROM aios.character_agent_runtime ar
            WHERE ar.state='ready'
              AND (ar.cooldown_until IS NULL OR ar.cooldown_until <= now())
              AND EXISTS (
                  SELECT 1 FROM aios.character_wake_event w
                  WHERE w.instance_id=ar.instance_id AND w.status='pending'
                    AND w.available_at <= now()
              )
              AND NOT EXISTS (
                  SELECT 1 FROM aios.character_cognitive_task t
                  WHERE t.instance_id=ar.instance_id
                    AND t.status IN ('queued','running','waiting')
              )
            ORDER BY ar.last_wake_at NULLS FIRST
            LIMIT $1
            """,
            max(1, min(int(limit), 1000)),
        )
        scheduled = direct
        for row in rows:
            if await self._schedule_instance(row["instance_id"]):
                scheduled += 1
        return scheduled

    async def _schedule_instance(self, instance_id: UUID) -> bool:
        events = await self.db.fetch(
            """
            SELECT wake_id,event_type,source_type,source_id,payload,priority
            FROM aios.character_wake_event
            WHERE instance_id=$1 AND status='pending' AND available_at <= now()
            ORDER BY priority,created_at LIMIT 32
            """,
            instance_id,
        )
        if not events:
            return False

        # Pure heartbeat with no other reason to think is intentionally cheap.
        if all(str(e["event_type"]) == "HEARTBEAT" for e in events):
            for event in events:
                await self.db.execute(
                    """UPDATE aios.character_wake_event SET status='consumed', consumed_at=now()
                       WHERE wake_id=$1 AND status='pending'""",
                    event["wake_id"],
                )
            await self.runtime.finish_semantic_turn(instance_id, semantic=False)
            return False

        # A delegated child completion resumes its existing parent task rather
        # than inventing another executive identity/task.
        child_events = [
            e for e in events
            if str(e["event_type"]) in {"COGNITIVE_TASK_COMPLETED","COGNITIVE_TASK_FAILED"}
            and isinstance(e["payload"], dict) and e["payload"].get("parent_task_id")
        ]
        if child_events:
            event = child_events[0]
            parent_id = UUID(str(event["payload"]["parent_task_id"]))
            parent = await self.agency.get_task(parent_id)
            if parent and parent.status in {"waiting","running"}:
                if parent.status == "running":
                    parent = await self.agency.transition_task(parent_id, "waiting")
                await self.agency.transition_task(parent_id, "queued")
                await self.db.execute(
                    """UPDATE aios.character_cognitive_task
                       SET retrieval_focus=COALESCE(retrieval_focus,'') || $2,
                           trigger_type='cognitive_resume',
                           resume_count=resume_count+1, updated_at=now()
                       WHERE task_id=$1""",
                    parent_id,
                    "\n\nDELEGATED COGNITION RESULT:\n" + json.dumps(event["payload"], default=str),
                )
                await self.db.execute(
                    """UPDATE aios.character_wake_event SET status='consumed',consumed_at=now()
                       WHERE wake_id=$1 AND status='pending'""", event["wake_id"],
                )
                return True

        # Ordinary accumulated host experience is first projected through the
        # graph/epistemic machinery into evidence-backed possible thoughts.
        # Only ambiguous character-relative attention reaches the tiny LLM.
        delta_events=[e for e in events if str(e["event_type"])=="COGNITIVE_DELTA_READY"]
        if delta_events:
            event=delta_events[-1]
            payload=event["payload"] if isinstance(event["payload"],dict) else {}
            def _delta_uuid(value):
                try: return UUID(str(value)) if value else None
                except (TypeError,ValueError): return None
            from .opportunity_router import OpportunityRouter
            tx_id=await OpportunityRouter(self.db).admit(
                instance_id=instance_id,
                source_node_id=_delta_uuid(payload.get("source_through_node_id")))
            for item in delta_events:
                await self.db.execute(
                    """UPDATE aios.character_wake_event SET status='consumed',consumed_at=now()
                       WHERE wake_id=$1 AND status='pending'""",item["wake_id"])
            if tx_id is not None:
                # The episode remains pending until the selected cognitive operation
                # reaches a terminal state. Consuming the wake only means routing
                # succeeded; it is not cognition completion.
                await self.runtime.finish_semantic_turn(instance_id,semantic=False)
                return True
            # No worthwhile opportunity: advance the background cognition cursor
            # without paying for an executive LLM call.
            from .admission import AutonomyAdmissionService
            await AutonomyAdmissionService(self.db).mark_episode_succeeded(
                instance_id=instance_id,
                through_node_id=_delta_uuid(payload.get("source_through_node_id")))
            await self.runtime.finish_semantic_turn(instance_id,semantic=False)
            return False

        ids = [str(e["wake_id"]) for e in events]
        summaries = [
            {
                "event_type": str(e["event_type"]),
                "source_type": e["source_type"],
                "source_id": e["source_id"],
                "payload": e["payload"],
            }
            for e in events
        ]
        objective = "Review the pending events and decide the next bounded action."
        provenance = next(
            (
                e["payload"] for e in reversed(events)
                if str(e["event_type"]) == "COGNITIVE_DELTA_READY"
                and isinstance(e["payload"], dict)
            ),
            {},
        )
        def _uuid(value):
            try:
                return UUID(str(value)) if value else None
            except (TypeError, ValueError):
                return None
        through_node = _uuid(provenance.get("source_through_node_id"))
        task = await self.agency.create_task(
            instance_id=instance_id, task_type="executive", objective=objective,
            hud_profile_name="agent.executive", retrieval_focus=json.dumps(summaries, default=str),
            priority=min(int(e["priority"]) for e in events), trigger_type="wake_batch",
            trigger_id=",".join(ids), execution_mode="auto",
            source_state_version=provenance.get("source_state_version"),
            source_node_id=through_node,
            source_from_node_id=_uuid(provenance.get("source_from_node_id")),
            source_through_node_id=through_node,
            meta={"wake_ids": ids, "events": summaries,
                  "host_cognition": bool(provenance.get("host_cognition", False))},
        )
        await self.db.execute(
            """UPDATE aios.character_wake_event SET status='claimed', claimed_at=now()
               WHERE wake_id = ANY($1::uuid[]) AND status='pending'""",
            [UUID(v) for v in ids],
        )
        job_id = await enqueue_job(
            self.db, job_type="agent_wake",
            payload={"instance_id": str(instance_id), "task_id": str(task.task_id)},
            priority=task.priority,
        )
        if job_id is None:
            await self.db.execute(
                """UPDATE aios.character_wake_event SET status='pending', claimed_at=NULL
                   WHERE wake_id = ANY($1::uuid[]) AND status='claimed'""",
                [UUID(v) for v in ids],
            )
            await self.agency.transition_task(task.task_id, "cancelled")
            return False
        return True

    async def consume_task_wakes(self, task_id: UUID) -> None:
        task = await self.agency.get_task(task_id)
        if not task:
            return
        wake_ids = [UUID(v) for v in task.meta.get("wake_ids", [])]
        if wake_ids:
            await self.db.execute(
                """UPDATE aios.character_wake_event SET status='consumed', consumed_at=now()
                   WHERE wake_id = ANY($1::uuid[]) AND status='claimed'""",
                wake_ids,
            )
