"""Canonical AIOS ASGI entrypoint.

The established API surface lives in main_legacy. POST /ingest is replaced by
the idempotent implementation so exact active SillyTavern replays return before
DAG, runtime-cursor, HUD-dirty, and downstream pipeline side effects.
"""

from aios_app.main_legacy import app, db
from aios_app.ingest_api import ingest_message
from aios_app.models import IngestIn, IngestOut, CorpusDocumentIn, CorpusConsumeIn, CorpusSourceProfileIn, CorpusFacetRouteIn, CharacterKnowledgeDomainsIn, KnowledgeDomainIn, KnowledgeDomainIdentifierIn
from aios_app.char.identity_bootstrap import bootstrap_character_card
from aios_app.char.identity_revision import accept_identity_candidate, reject_identity_candidate
from aios_app.char.identity_sources import stage_identity_source, identity_snapshot
from aios_app.corpus import import_corpus_document, consume_corpus_sections
from aios_app.epistemic.research import CharacterResearchService
from aios_app.corpus_catalog import CorpusCatalogService, CorpusAccessReconciler
from aios_app.corpus_routing import ensure_facet_route, ensure_knowledge_domain, register_domain_identifier
from pydantic import BaseModel, Field
from typing import Any


app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == "/ingest"
        and "POST" in (getattr(route, "methods", None) or set())
    )
]


@app.post("/ingest", response_model=IngestOut)
async def ingest(req: IngestIn) -> IngestOut:
    return await ingest_message(db, req)


class CharacterCardBootstrapIn(BaseModel):
    card: dict[str, Any]
    source_name: str | None = None
    source_format: str = "character_card"
    replace_authored_facets: bool = True
    auto_accept_authored: bool = True


@app.post("/character/{character_id}/identity/bootstrap/card")
async def bootstrap_identity_card(
    character_id: str,
    req: CharacterCardBootstrapIn,
) -> dict[str, Any]:
    """Explicitly bootstrap slow-changing identity from an external card."""
    return await bootstrap_character_card(
        db,
        character_id=character_id,
        payload=req.card,
        source_name=req.source_name,
        source_format=req.source_format,
        replace_authored_facets=req.replace_authored_facets,
        auto_accept_authored=req.auto_accept_authored,
    )


class IdentityCandidateDecisionIn(BaseModel):
    reason: str | None = None
    actor: str = "api"


@app.post("/character/{character_id}/identity/candidate/{candidate_id}/accept")
async def accept_character_identity_candidate(
    character_id: str,
    candidate_id: str,
    req: IdentityCandidateDecisionIn,
) -> dict[str, Any]:
    candidate = await db.fetchrow(
        "SELECT character_id FROM aios.character_identity_candidate WHERE candidate_id=$1::uuid",
        candidate_id,
    )
    if not candidate or candidate["character_id"] != character_id:
        raise LookupError("identity candidate does not belong to character")
    return await accept_identity_candidate(
        db, candidate_id, actor=req.actor, reason=req.reason
    )


@app.post("/character/{character_id}/identity/candidate/{candidate_id}/reject")
async def reject_character_identity_candidate(
    character_id: str,
    candidate_id: str,
    req: IdentityCandidateDecisionIn,
) -> dict[str, Any]:
    candidate = await db.fetchrow(
        "SELECT character_id FROM aios.character_identity_candidate WHERE candidate_id=$1::uuid",
        candidate_id,
    )
    if not candidate or candidate["character_id"] != character_id:
        raise LookupError("identity candidate does not belong to character")
    await reject_identity_candidate(db, candidate_id, reason=req.reason)
    return {"character_id": character_id, "candidate_id": candidate_id, "disposition": "rejected"}


class IdentitySourceCandidateIn(BaseModel):
    facet_type: str
    facet_key: str
    value: Any
    stability: str = "core"
    authority: str | None = None
    mutability: str = "explicit"
    perspective: str = "unknown"
    continuity_key: str | None = None
    source_field: str | None = None
    source_fragment: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class IdentitySourceStageIn(BaseModel):
    source_type: str
    payload: dict[str, Any]
    candidates: list[IdentitySourceCandidateIn]
    source_name: str | None = None
    source_format: str | None = None
    authority: str = "reference"
    continuity_key: str | None = None
    meta: dict[str, Any] = {}


@app.post("/character/{character_id}/identity/source")
async def stage_character_identity_source(
    character_id: str,
    req: IdentitySourceStageIn,
) -> dict[str, Any]:
    return await stage_identity_source(
        db,
        character_id=character_id,
        source_type=req.source_type,
        payload=req.payload,
        candidates=[item.model_dump() for item in req.candidates],
        source_name=req.source_name,
        source_format=req.source_format,
        authority=req.authority,
        continuity_key=req.continuity_key,
        meta=req.meta,
    )


@app.get("/character/{character_id}/identity")
async def get_character_identity(character_id: str) -> dict[str, Any]:
    return await identity_snapshot(db, character_id)


@app.post("/corpus/document")
async def add_corpus_document(req: CorpusDocumentIn) -> dict[str, Any]:
    """Store cold searchable literature without semantic ingestion."""
    return await import_corpus_document(
        db,
        text=req.text,
        source_id=req.source_id,
        source_kind=req.source_kind,
        title=req.title,
        author=req.author,
        source_uri=req.source_uri,
        language=req.language,
        meta=req.meta,
    )


@app.post("/corpus/source-profile")
async def upsert_corpus_source_profile(req: CorpusSourceProfileIn) -> dict[str, Any]:
    """Create/update one trusted source route and optionally reclassify old documents."""
    catalog = CorpusCatalogService(db)
    profile = await catalog.ensure_profile(
        profile_key=req.profile_key,
        collection_key=req.collection_key,
        scope_key=req.scope_key,
        display_name=req.display_name,
        source_id=req.source_id,
        domain_pattern=req.domain_pattern,
        path_prefix=req.path_prefix,
        knowledge_domain=req.knowledge_domain,
        access_class=req.access_class,
        epistemic_namespace=req.epistemic_namespace,
        identity_binding=req.identity_binding,
        priority=req.priority,
        meta=req.meta,
    )
    result: dict[str, Any] = {"profile": profile}
    if req.reclassify_existing:
        result["reclassification"] = await catalog.reclassify_profile(req.profile_key)
    if req.knowledge_domain:
        result["access_reconciliation"] = await CorpusAccessReconciler(db).reconcile_domain(req.knowledge_domain)
    return result


@app.get("/corpus/source-profiles")
async def list_corpus_source_profiles() -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT csp.profile_id, csp.profile_key, csp.source_id, csp.domain_pattern, csp.path_prefix, csp.knowledge_domain,
               csp.collection_key, csp.scope_key, cs.access_class, csp.epistemic_namespace, csp.identity_binding, csp.priority,
               csp.enabled, csp.meta, csp.created_at, csp.updated_at
        FROM aios.corpus_source_profile csp
        JOIN aios.corpus_scope cs ON cs.scope_key=csp.scope_key
        ORDER BY csp.priority DESC, csp.profile_key
        """
    )
    return {"profiles": [dict(row) for row in rows]}


@app.post("/corpus/knowledge-domain")
async def upsert_knowledge_domain(req: KnowledgeDomainIn) -> dict[str, Any]:
    return await ensure_knowledge_domain(
        db,
        domain_key=req.domain_key,
        display_name=req.display_name,
        domain_kind=req.domain_kind,
        parent_domain_key=req.parent_domain_key,
        default_scope_key=req.default_scope_key,
        default_epistemic_namespace=req.default_epistemic_namespace,
        meta=req.meta,
    )


@app.get("/corpus/knowledge-domains")
async def list_knowledge_domains() -> dict[str, Any]:
    rows = await db.fetch(
        """SELECT kd.domain_id, kd.domain_key, kd.display_name, kd.domain_kind,
                  parent.domain_key AS parent_domain_key, kd.default_scope_key,
                  kd.default_epistemic_namespace, kd.enabled, kd.meta,
                  kd.created_at, kd.updated_at
           FROM aios.knowledge_domain kd
           LEFT JOIN aios.knowledge_domain parent
             ON parent.domain_id=kd.parent_domain_id
           ORDER BY kd.domain_key"""
    )
    return {"domains": [dict(row) for row in rows]}


@app.post("/corpus/knowledge-domain/identifier")
async def upsert_knowledge_domain_identifier(req: KnowledgeDomainIdentifierIn) -> dict[str, Any]:
    return await register_domain_identifier(
        db,
        domain_key=req.domain_key,
        identifier_type=req.identifier_type,
        identifier_value=req.identifier_value,
        source=req.source,
        confidence=req.confidence,
        meta=req.meta,
    )


@app.get("/corpus/knowledge-domain-candidates")
async def list_knowledge_domain_candidates(status: str = "unresolved") -> dict[str, Any]:
    rows = await db.fetch(
        """SELECT candidate_id, identifier_type, identifier_value, status,
                  occurrence_count, first_document_id, last_document_id,
                  resolved_domain_id, source, meta, first_seen_at, last_seen_at,
                  resolved_at
           FROM aios.knowledge_domain_candidate
           WHERE ($1='' OR status=$1)
           ORDER BY occurrence_count DESC, last_seen_at DESC""",
        status,
    )
    return {"candidates": [dict(row) for row in rows]}


@app.get("/character/domain-candidates")
async def list_character_domain_candidates(
    status: str = "unresolved",
    character_id: str | None = None,
) -> dict[str, Any]:
    rows = await db.fetch(
        """SELECT cdc.candidate_id, cdc.character_id, cdc.source_id,
                  cdc.identifier_type, cdc.identifier_value, cdc.relationship,
                  cdc.status, kd.domain_key AS resolved_domain,
                  cdc.source_field, cdc.meta, cdc.created_at, cdc.resolved_at
           FROM aios.character_domain_candidate cdc
           LEFT JOIN aios.knowledge_domain kd
             ON kd.domain_id=cdc.resolved_domain_id
           WHERE ($1='' OR cdc.status=$1)
             AND ($2::text IS NULL OR cdc.character_id=$2)
           ORDER BY cdc.created_at DESC""",
        status, character_id,
    )
    return {"candidates": [dict(row) for row in rows]}


@app.post("/corpus/facet-route")
async def upsert_corpus_facet_route(req: CorpusFacetRouteIn) -> dict[str, Any]:
    """Register one trusted structured-facet to document-domain route."""
    return await ensure_facet_route(
        db,
        facet_type=req.facet_type,
        facet_value=req.facet_value,
        knowledge_domain=req.knowledge_domain,
        scope_key=req.scope_key,
        epistemic_namespace=req.epistemic_namespace,
        access_class=req.access_class,
        priority=req.priority,
        meta=req.meta,
    )


@app.get("/corpus/facet-routes")
async def list_corpus_facet_routes() -> dict[str, Any]:
    rows = await db.fetch(
        """SELECT facet_type, facet_value, knowledge_domain, scope_key,
                  epistemic_namespace, access_class, priority, enabled, meta
           FROM aios.corpus_facet_route
           ORDER BY priority DESC, facet_type, facet_value, knowledge_domain"""
    )
    return {"routes": [dict(row) for row in rows]}


@app.put("/character/{character_id}/corpus/knowledge-domains")
async def set_character_corpus_knowledge_domains(character_id: str, req: CharacterKnowledgeDomainsIn) -> dict[str, Any]:
    return await CorpusAccessReconciler(db).set_character_domains(character_id, req.knowledge_domains)


@app.post("/character/{character_id}/corpus/reconcile-access")
async def reconcile_character_corpus_access(character_id: str) -> dict[str, Any]:
    return await CorpusAccessReconciler(db).reconcile_character(character_id)


@app.post("/instance/{instance_id}/corpus/consume")
async def consume_corpus(instance_id: str, req: CorpusConsumeIn) -> dict[str, Any]:
    """Intentionally cross cold corpus sections into this actor's knowledge path."""
    from uuid import UUID
    return await CharacterResearchService(db).acquire(
        instance_id=UUID(instance_id),
        section_ids=req.section_ids,
        mode=req.mode,
    )


# -------------------------------------------------
# Donated inference worker control plane
# -------------------------------------------------

from uuid import UUID
from aios_app.inference import InferenceBroker, InferenceProviderStore


class InferenceProviderIn(BaseModel):
    provider_key: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str | None = None
    enabled: bool = True
    drain: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=64)
    context_window: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    worker_classes: list[str] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class InferenceProviderControlIn(BaseModel):
    enabled: bool | None = None
    drain: bool | None = None


def _provider_dict(provider) -> dict[str, Any]:
    return {
        "provider_id": str(provider.provider_id),
        "provider_key": provider.provider_key,
        "display_name": provider.display_name,
        "base_url": provider.base_url,
        "model": provider.model,
        "api_key_env": provider.api_key_env,
        "enabled": provider.enabled,
        "drain": provider.drain,
        "max_concurrency": provider.max_concurrency,
        "context_window": provider.context_window,
        "timeout_seconds": provider.timeout_seconds,
        "worker_classes": list(provider.worker_classes),
        "capabilities": provider.capabilities,
        "status": provider.status,
        "consecutive_failures": provider.consecutive_failures,
        "cooldown_until": str(provider.cooldown_until or ""),
        "last_error": provider.last_error,
    }


@app.get("/inference/providers")
async def list_inference_providers() -> dict[str, Any]:
    providers = await InferenceProviderStore(db).list()
    return {"providers": [_provider_dict(p) for p in providers]}


@app.post("/inference/providers")
async def upsert_inference_provider(req: InferenceProviderIn) -> dict[str, Any]:
    provider = await InferenceProviderStore(db).upsert(**req.model_dump())
    return _provider_dict(provider)


@app.patch("/inference/providers/{provider_id}")
async def control_inference_provider(
    provider_id: UUID, req: InferenceProviderControlIn
) -> dict[str, Any]:
    provider = await InferenceProviderStore(db).set_control(
        provider_id, enabled=req.enabled, drain=req.drain
    )
    return _provider_dict(provider)


@app.post("/inference/providers/{provider_id}/health")
async def health_inference_provider(provider_id: UUID) -> dict[str, Any]:
    ok = await InferenceBroker(db).health_check(provider_id)
    provider = await InferenceProviderStore(db).get(provider_id)
    return {"ok": ok, "provider": _provider_dict(provider) if provider else None}


# -------------------------------------------------
# Character agent task / wake / heartbeat API
# -------------------------------------------------

from aios_app.agent import AgentRuntimeStore, CharacterAgencyStore, CharacterWorker


class CognitiveTaskIn(BaseModel):
    task_type: str = Field(pattern="^(executive|research|planning|reflection|communication)$")
    objective: str = Field(min_length=1)
    hud_profile_name: str | None = None
    retrieval_focus: str | None = None
    priority: int = 100


class WakeEventIn(BaseModel):
    event_type: str = Field(min_length=1)
    source_type: str | None = None
    source_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    dedupe_key: str | None = None


class HeartbeatControlIn(BaseModel):
    enabled: bool
    interval_seconds: int = Field(default=300, ge=60)


@app.post("/agent/instance/{instance_id}/task")
async def create_agent_task(instance_id: UUID, req: CognitiveTaskIn) -> dict[str, Any]:
    task = await CharacterAgencyStore(db).create_task(
        instance_id=instance_id, task_type=req.task_type, objective=req.objective,
        hud_profile_name=req.hud_profile_name, retrieval_focus=req.retrieval_focus,
        priority=req.priority, trigger_type="api",
    )
    await AgentRuntimeStore(db).wake(
        instance_id=instance_id, event_type="TASK_ASSIGNED",
        source_type="task", source_id=str(task.task_id),
        payload={"task_id": str(task.task_id), "task_type": task.task_type},
        dedupe_key=f"task:{task.task_id}",
    )
    return {"task_id": str(task.task_id), "status": task.status}


@app.post("/agent/task/{task_id}/run")
async def run_agent_task(task_id: UUID) -> dict[str, Any]:
    return await CharacterWorker(db).run_task(task_id)


@app.post("/agent/instance/{instance_id}/wake")
async def wake_agent(instance_id: UUID, req: WakeEventIn) -> dict[str, Any]:
    wake_id = await AgentRuntimeStore(db).wake(
        instance_id=instance_id, event_type=req.event_type,
        source_type=req.source_type, source_id=req.source_id,
        payload=req.payload, priority=req.priority, dedupe_key=req.dedupe_key,
    )
    return {"wake_id": str(wake_id), "status": "pending"}


@app.put("/agent/instance/{instance_id}/heartbeat")
async def configure_agent_heartbeat(
    instance_id: UUID, req: HeartbeatControlIn
) -> dict[str, Any]:
    await AgentRuntimeStore(db).configure_heartbeat(
        instance_id, enabled=req.enabled, interval_seconds=req.interval_seconds
    )
    return {
        "instance_id": str(instance_id), "heartbeat_enabled": req.enabled,
        "heartbeat_interval_seconds": req.interval_seconds,
    }


@app.post("/agent/heartbeats/emit")
async def emit_agent_heartbeats(limit: int = 100) -> dict[str, Any]:
    count = await AgentRuntimeStore(db).emit_due_heartbeats(limit=limit)
    return {"emitted": count}
