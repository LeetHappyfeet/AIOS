from __future__ import annotations

from typing import Any, Literal
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field
from aios_app.agent.gateway import ExternalGateway
from aios_app.agent.policy import ActionPolicyService
from aios_app.agent.actions import ActionDispatcher, default_action_registry
from aios_app.agent.lifecycle import CharacterAgencyStore
from aios_app.agent.remote_workers import RemoteWorkerStore


class IntegrationIn(BaseModel):
    integration_key: str
    integration_type: str
    display_name: str
    base_url: str | None = None
    auth_env: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class ExternalEventIn(BaseModel):
    external_event_id: str
    instance_id: UUID
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ActionPolicyIn(BaseModel):
    disposition: str
    actor: str = "operator"


class ApprovalIn(BaseModel):
    approved: bool
    actor: str = "operator"
    reason: str | None = None
    worker_class: str = "executive"


class RemoteWorkerIn(BaseModel):
    worker_key: str = Field(min_length=1)
    worker_type: str = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    labels: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int = Field(default=1, ge=1, le=256)


class RemoteWorkerClaimIn(BaseModel):
    lease_seconds: int = Field(default=120, ge=30, le=3600)


class RemoteWorkerResultIn(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)


class RemoteWorkerFailureIn(BaseModel):
    error: str = Field(min_length=1, max_length=2000)


def _action_payload(action) -> dict[str, Any]:
    return {
        "action_id": str(action.action_id),
        "instance_id": str(action.instance_id),
        "task_id": str(action.task_id or ""),
        "action_type": action.action_type,
        "arguments": action.arguments,
        "side_effect_class": action.side_effect_class,
        "result_mode": action.result_mode,
        "execution_mode": action.execution_mode,
        "status": action.status,
        "lease_expires_at": str(action.lease_expires_at or ""),
    }


class GoalRetryIn(BaseModel):
    request_id: UUID


class ParticipationExperimentIn(BaseModel):
    since_at: datetime | None = None
    duration_minutes: int = Field(default=60, ge=1, le=1440)
    max_claims: int = Field(default=100, ge=1, le=1000)


class OutcomeCorrectionIn(BaseModel):
    request_id: UUID
    reason: str = Field(min_length=1, max_length=500)



class ResearchToolRequestIn(BaseModel):
    source_node_id: UUID


class ResearchOpenIn(BaseModel):
    question: str = Field(min_length=3,max_length=600)
    topic_id: UUID | None = None
    max_cycles: int = Field(default=8,ge=1,le=16)
    max_sections: int = Field(default=24,ge=1,le=48)
    max_materializations: int = Field(default=4,ge=0,le=8)


class ResearchAdvanceIn(BaseModel):
    request_id: UUID
    include_fanwork: bool = False


class ResearchQuestionIn(BaseModel):
    question: str = Field(min_length=3,max_length=600)


class ResearchStudyIn(BaseModel):
    request_id: UUID
    section_ids: list[UUID] = Field(min_length=1,max_length=2)


class ResearchStatusIn(BaseModel):
    status: Literal["open","paused","closed"]


def install_external_agency_routes(app, db) -> None:
    @app.get("/agent/knowledge-atlas/health")
    async def knowledge_atlas_health():
        from fastapi import HTTPException
        from aios_app.topic_atlas.diagnostics import atlas_health
        if db is None:
            raise HTTPException(503,"Runtime database not connected")
        return await atlas_health(db)



    # Research operations are instance-scoped and never write directly into /char.
    # Every source-study submission is reauthorized by CharacterResearchService.
    @app.post("/agent/instance/{instance_id}/research/tool-request")
    async def dispatch_source_research(instance_id: UUID, req: ResearchToolRequestIn):
        """Validate actual character source text and dedupe transcript replay."""
        import hashlib
        import json
        from fastapi import HTTPException
        from aios_app.agent.research_action import extract_research_request
        from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
        source=await db.fetchrow(
            """SELECT dn.message_text,dn.speaker_role::text AS role,
                      dn.speaker_id,ci.character_id
               FROM aios.character_runtime_state rs
               JOIN aios.character_instance ci ON ci.instance_id=rs.instance_id
               JOIN aios.dag_node dn ON dn.node_id=$2
                 AND dn.timeline_id=rs.source_timeline_id
                 AND dn.node_id=rs.source_head_node_id
               WHERE rs.instance_id=$1""",instance_id,req.source_node_id)
        if not source or source["role"]!='character' or (
            str(source["speaker_id"])!=str(source["character_id"])):
            raise HTTPException(409,"research request must be the current character-authored source node")
        try:
            question=extract_research_request(source["message_text"])
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        if question is None:
            raise HTTPException(422,"source does not contain a research operation")
        digest=hashlib.sha256(question.encode("utf-8")).hexdigest()
        async with db.connection() as con:
            async with con.transaction():
                # Serialize duplicate calls from render, reconciliation and swipe sync.
                await con.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"research-tool:{instance_id}:{req.source_node_id}")
                old=await con.fetchrow(
                    """SELECT request_hash,status,result FROM aios.character_research_tool_request
                       WHERE instance_id=$1 AND source_node_id=$2 FOR UPDATE""",
                    instance_id,req.source_node_id)
                if old and old["request_hash"]!=digest:
                    raise HTTPException(409,"source action changed; use a new DAG source coordinate")
                if old and old["status"]=="completed":
                    saved=old["result"]
                    return dict(saved) if isinstance(saved,dict) else json.loads(saved)
                if not old:
                    await con.execute(
                        """INSERT INTO aios.character_research_tool_request
                           (instance_id,source_node_id,request_hash,question)
                           VALUES($1,$2,$3,$4)""",instance_id,req.source_node_id,digest,question)
        service=ProgressiveResearchService(db)
        try:
            dossier=await service.start(instance_id=instance_id,question=question,origin="cognition")
            result=await service.advance(
                instance_id=instance_id,dossier_id=UUID(dossier["dossier_id"]),
                request_id=req.source_node_id,include_fanwork=False)
            output={"operation":"research","dossier_id":dossier["dossier_id"],
                    "source_node_id":str(req.source_node_id),
                    "status":result.get("status"),
                    "research_id":result.get("research_id"),
                    "source_count":result.get("source_count",0),
                    "new_sources":result.get("new_sources",0),
                    "follow_up_questions":result.get("follow_up_questions",[]),
                    "durable_knowledge":False}
            await db.execute(
                """UPDATE aios.character_research_tool_request
                   SET dossier_id=$3,research_id=$4,status='completed',
                       result=$5::jsonb,error=NULL,updated_at=now()
                   WHERE instance_id=$1 AND source_node_id=$2""",
                instance_id,req.source_node_id,UUID(dossier["dossier_id"]),
                UUID(str(result["research_id"])) if result.get("research_id") else None,
                json.dumps(output))
            return output
        except Exception as exc:
            await db.execute(
                """UPDATE aios.character_research_tool_request
                   SET status='failed',error=$3,updated_at=now()
                   WHERE instance_id=$1 AND source_node_id=$2""",
                instance_id,req.source_node_id,str(exc)[:900])
            raise

    @app.post("/agent/instance/{instance_id}/research")
    async def open_research_dossier(instance_id: UUID, req: ResearchOpenIn):
        from fastapi import HTTPException
        from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
        try:
            return await ProgressiveResearchService(db).start(
                instance_id=instance_id,question=req.question,topic_id=req.topic_id,
                origin="manual",max_cycles=req.max_cycles,max_sections=req.max_sections,
                max_materializations=req.max_materializations)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.get("/agent/instance/{instance_id}/research")
    async def list_research_dossiers(instance_id: UUID, limit: int = 12):
        from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
        return {"dossiers":await ProgressiveResearchService(db).list_dossiers(
            instance_id=instance_id,limit=limit)}

    @app.get("/agent/instance/{instance_id}/research/{dossier_id}")
    async def inspect_research_dossier(instance_id: UUID, dossier_id: UUID):
        from fastapi import HTTPException
        from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
        try:
            return await ProgressiveResearchService(db).inspect(
                instance_id=instance_id,dossier_id=dossier_id)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc

    @app.post("/agent/instance/{instance_id}/research/{dossier_id}/advance")
    async def advance_research_dossier(
        instance_id: UUID, dossier_id: UUID, req: ResearchAdvanceIn
    ):
        from fastapi import HTTPException
        from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
        try:
            return await ProgressiveResearchService(db).advance(
                instance_id=instance_id,dossier_id=dossier_id,
                request_id=req.request_id,include_fanwork=req.include_fanwork)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.post("/agent/instance/{instance_id}/research/{dossier_id}/question")
    async def extend_research_dossier(
        instance_id: UUID, dossier_id: UUID, req: ResearchQuestionIn
    ):
        from fastapi import HTTPException
        from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
        try:
            return await ProgressiveResearchService(db).add_question(
                instance_id=instance_id,dossier_id=dossier_id,question=req.question)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.post("/agent/instance/{instance_id}/research/{dossier_id}/study")
    async def study_research_sections(
        instance_id: UUID, dossier_id: UUID, req: ResearchStudyIn
    ):
        from fastapi import HTTPException
        from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
        try:
            return await ProgressiveResearchService(db).study(
                instance_id=instance_id,dossier_id=dossier_id,
                request_id=req.request_id,section_ids=req.section_ids)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.post("/agent/instance/{instance_id}/research/{dossier_id}/status")
    async def change_research_dossier_status(
        instance_id: UUID, dossier_id: UUID, req: ResearchStatusIn
    ):
        from fastapi import HTTPException
        from aios_app.topic_atlas.research_dossier import ProgressiveResearchService
        try:
            return await ProgressiveResearchService(db).set_status(
                instance_id=instance_id,dossier_id=dossier_id,status=req.status)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.get("/agent/runtime/versions")
    async def inspect_effective_runtime_versions():
        """Installed code, effective SQL policy and receipt provenance are separate."""
        from aios_app.epistemic.runtime_versions import capture_runtime_manifest
        if db is None:
            # Tests may install routes without an attached DB. Do not
            # fabricate active authority in that case.
            from fastapi import HTTPException
            raise HTTPException(503, "Runtime database not connected")
        return await capture_runtime_manifest(db)

    @app.get("/agent/instance/{instance_id}/participation/context")
    async def audit_participation_context(instance_id: UUID):
        from fastapi import HTTPException
        from .participation import ParticipationService
        try:
            return await ParticipationService(db).audit_context(instance_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/agent/instance/{instance_id}/participation/experiments")
    async def start_participation(instance_id: UUID, req: ParticipationExperimentIn):
        from fastapi import HTTPException
        from .participation import ParticipationService
        try:
            return await ParticipationService(db).start(instance_id, **req.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            # No shadow evaluator: fail enrollment visibly rather than
            # accepting an experiment that can never produce comparisons.
            raise HTTPException(503, str(exc)) from exc

    @app.get("/agent/instance/{instance_id}/participation/experiments/{experiment_id}")
    async def inspect_participation(instance_id: UUID, experiment_id: UUID, limit: int = 50):
        from fastapi import HTTPException
        from .participation import ParticipationService
        try:
            return await ParticipationService(db).inspect(instance_id, experiment_id, limit=limit)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/agent/instance/{instance_id}/participation/experiments/{experiment_id}/compare")
    async def compare_participation(instance_id: UUID, experiment_id: UUID, limit: int = 50):
        from fastapi import HTTPException
        from .participation import ParticipationService
        try:
            return await ParticipationService(db).compare_existing(instance_id, experiment_id, limit=limit)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/agent/instance/{instance_id}/participation/experiments/{experiment_id}/stop")
    async def stop_participation(instance_id: UUID, experiment_id: UUID):
        from fastapi import HTTPException
        from .participation import ParticipationService
        try:
            await ParticipationService(db).stop(instance_id, experiment_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"shadow": True, "status": "stopped"}

    @app.get("/agent/instance/{instance_id}/reinforcement")
    async def inspect_reinforcement(instance_id: UUID, limit: int = 50):
        from .reinforcement import ShadowReinforcementService
        return await ShadowReinforcementService(db).inspect(instance_id, limit=limit)

    @app.post("/agent/instance/{instance_id}/goal/{goal_id}/retry")
    async def retry_goal(instance_id: UUID, goal_id: UUID, req: GoalRetryIn):
        from fastapi import HTTPException
        from aios_app.epistemic.goals import CharacterGoalService
        try:
            goal = await CharacterGoalService(db).retry(instance_id=instance_id, goal_id=goal_id, request_id=req.request_id)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"goal_id": str(goal.goal_id), "status": goal.status}

    @app.post("/agent/instance/{instance_id}/outcome/{outcome_id}/invalidate")
    async def invalidate_outcome(instance_id: UUID, outcome_id: UUID, req: OutcomeCorrectionIn):
        from fastapi import HTTPException
        from .outcomes import OutcomeResolver
        try:
            corrected = await OutcomeResolver(db).invalidate(instance_id=instance_id, outcome_id=outcome_id,
                                                              request_id=req.request_id, reason=req.reason)
        except (LookupError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"outcome_id": str(corrected), "shadow": True}

    @app.post("/agent/workers")
    async def register_remote_worker(req: RemoteWorkerIn):
        row = await RemoteWorkerStore(db).register(**req.model_dump())
        return {
            "worker_id": str(row["worker_id"]),
            "worker_key": row["worker_key"],
            "worker_type": row["worker_type"],
            "capabilities": list(row["capabilities"] or []),
            "max_concurrency": row["max_concurrency"],
            "status": row["status"],
        }

    @app.post("/agent/workers/{worker_id}/heartbeat")
    async def heartbeat_remote_worker(worker_id: UUID):
        row = await RemoteWorkerStore(db).heartbeat(worker_id)
        return {"worker_id": str(row["worker_id"]), "status": row["status"]}

    @app.post("/agent/workers/{worker_id}/claim")
    async def claim_remote_work(worker_id: UUID, req: RemoteWorkerClaimIn):
        action = await RemoteWorkerStore(db).claim(
            worker_id, lease_seconds=req.lease_seconds
        )
        return {"action": _action_payload(action) if action else None}

    @app.post("/agent/workers/{worker_id}/action/{action_id}/complete")
    async def complete_remote_work(
        worker_id: UUID, action_id: UUID, req: RemoteWorkerResultIn
    ):
        action = await RemoteWorkerStore(db).complete(worker_id, action_id, req.result)
        return _action_payload(action)

    @app.post("/agent/workers/{worker_id}/action/{action_id}/fail")
    async def fail_remote_work(
        worker_id: UUID, action_id: UUID, req: RemoteWorkerFailureIn
    ):
        action = await RemoteWorkerStore(db).fail(worker_id, action_id, req.error)
        return _action_payload(action)

    @app.post("/agent/integrations")
    async def register_integration(req: IntegrationIn):
        return await ExternalGateway(db).register(**req.model_dump())

    @app.post("/agent/integrations/{integration_key}/event")
    async def ingest_external_event(integration_key: str, req: ExternalEventIn):
        receipt = await ExternalGateway(db).ingest_event(
            integration_key=integration_key, **req.model_dump()
        )
        return {"receipt_id": str(receipt)}

    @app.put("/agent/instance/{instance_id}/policy/{action_type:path}")
    async def set_action_policy(instance_id: UUID, action_type: str, req: ActionPolicyIn):
        await ActionPolicyService(db).set_policy(
            instance_id, action_type, req.disposition, req.actor
        )
        return {"updated": True}

    @app.post("/agent/action/{action_id}/approval")
    async def decide_action(action_id: UUID, req: ApprovalIn):
        policy = ActionPolicyService(db)
        await policy.decide(action_id, req.approved, req.actor, req.reason)
        if not req.approved:
            action = await CharacterAgencyStore(db).transition_action(
                action_id, "rejected", rejection_reason="operator_denied"
            )
        else:
            action = await ActionDispatcher(db, default_action_registry(db)).dispatch(
                action_id, worker_class=req.worker_class
            )
        return {"action_id": str(action.action_id), "status": action.status}

    @app.post("/agent/delivery/{delivery_id}/run")
    async def run_external_delivery(delivery_id: UUID):
        return await ExternalGateway(db).deliver(delivery_id)
