"""Canonical AIOS ASGI entrypoint.

The established API surface lives in main_legacy. POST /ingest is replaced by
the idempotent implementation so exact active SillyTavern replays return before
DAG, runtime-cursor, HUD-dirty, and downstream pipeline side effects.
"""

from aios_app.main_legacy import app, db
from aios_app.ingest_api import ingest_message
from aios_app.models import IngestIn, IngestOut, CorpusDocumentIn, CorpusConsumeIn, CorpusSourceProfileIn
from aios_app.char.identity_bootstrap import bootstrap_character_card
from aios_app.char.identity_revision import accept_identity_candidate, reject_identity_candidate
from aios_app.char.identity_sources import stage_identity_source, identity_snapshot
from aios_app.corpus import import_corpus_document, consume_corpus_sections
from aios_app.corpus_catalog import CorpusCatalogService
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
        epistemic_namespace=req.epistemic_namespace,
        identity_binding=req.identity_binding,
        priority=req.priority,
        meta=req.meta,
    )
    result: dict[str, Any] = {"profile": profile}
    if req.reclassify_existing:
        result["reclassification"] = await catalog.reclassify_profile(req.profile_key)
    return result


@app.get("/corpus/source-profiles")
async def list_corpus_source_profiles() -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT profile_id, profile_key, source_id, domain_pattern, collection_key,
               scope_key, epistemic_namespace, identity_binding, priority,
               enabled, meta, created_at, updated_at
        FROM aios.corpus_source_profile
        ORDER BY priority DESC, profile_key
        """
    )
    return {"profiles": [dict(row) for row in rows]}


@app.post("/instance/{instance_id}/corpus/consume")
async def consume_corpus(instance_id: str, req: CorpusConsumeIn) -> dict[str, Any]:
    """Intentionally cross cold corpus sections into this actor's knowledge path."""
    from uuid import UUID
    return await consume_corpus_sections(
        db,
        instance_id=UUID(instance_id),
        section_ids=req.section_ids,
        mode=req.mode,
    )
