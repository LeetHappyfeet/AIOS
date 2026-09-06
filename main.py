from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional, Dict, Any
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from aios_app.config import settings
from aios_app.db import Database
from aios_app.models import (
    SessionCreate,
    SessionOut,
    IngestIn,
    IngestOut,
    ExternalObservationIn,
    MemoryOut,
    CharacterActivateIn,
    CharacterActivateOut,
    WorldEntityCreateIn,
    WorldRelationCreateIn,
    WorldRulePutIn,
    WorldActionIn,
    CharacterForkIn,
    EntityControllerIn,
    KnowledgeAcquireIn,
    GeneratedFactIn,
    WorldObservedFactIn,
    LongDocumentIn,
    CharacterEpistemicProfileIn,
    DocumentAcquireIn,
    EpistemicSearchIn,
)
from aios_app.dag import get_or_create_timeline, add_node_and_edge
from aios_app.memory import recent_nodes_as_memory, pick_latest_timeline_for_character
from aios_app.world.runtime import WorldRuntimeService, RuntimeConflict, RuntimeNotFound
from aios_app.epistemic.knowledge import record_acquisition
from aios_app.epistemic.generated import (
    create_generated_fact,
    assert_claim_or_proposition_in_world,
)
from aios_app.epistemic.query import proposition_context, world_epistemic_state
from aios_app.documents.long_document import ingest_long_document
from aios_app.epistemic.weights import (
    get_profile,
    upsert_profile,
    reweight_character_knowledge,
)
from aios_app.epistemic.knowledge import acquire_document
from aios_app.epistemic.search import epistemic_search, document_epistemic_summary
from aios_app.external_observation import persist_external_observation
from aios_app.hud.readiness import mark_matching_runtime_dirty
from aios_app.hud.render_text import render_hud_text

logger = logging.getLogger("aios.main")

# =================================================
# App setup
# =================================================

app = FastAPI(title="AIOS MemoryVault", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = Database(settings.db_dsn)
world_runtime = WorldRuntimeService(db)

# =================================================
# Lifecycle
# =================================================

@app.on_event("startup")
async def startup() -> None:
    await db.connect()
    await world_runtime.hud.plugin_manager.startup()
    logger.info("Database connected")


@app.on_event("shutdown")
async def shutdown() -> None:
    await world_runtime.hud.plugin_manager.shutdown()
    await db.close()
    logger.info("Database closed")


# =================================================
# Health
# =================================================

@app.get("/healthz")
async def healthz():
    return {"ok": True}


# =================================================
# Session management
# =================================================

@app.post("/session", response_model=SessionOut)
async def create_session(req: SessionCreate) -> SessionOut:
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.session (source, source_session_id, topic, meta)
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING session_id, topic
        """,
        req.source or settings.source_name,
        req.source_session_id,
        req.topic,
        json.dumps(req.meta or {}),
    )

    return SessionOut(
        session_id=row["session_id"],
        topic=row["topic"],
    )


# =================================================
# Ingest (UNSTRUCTURED → DAG → downstream pipeline)
# =================================================

@app.post("/ingest", response_model=IngestOut)
async def ingest(req: IngestIn) -> IngestOut:
    """
    Persist raw message content and anchor it to the temporal DAG.

    Successful HTTP ingestion means the durable SQL event and DAG node exist.
    It does NOT mean downstream RDF processing is finished. The supervisor and
    runner advance the same ingest_event through section, claim, and RDF stage
    latches; process_status becomes 'done' only after RDF acknowledgement.

    Epistemic rule:
      - ALL chat / agent ingests are assigned to the LIMINAL world
      - World promotion happens later via logic, not user claims
    """

    # Resolve first-person identity exactly once at ingress. character_id is the
    # active character context; it is not assumed to be the physical speaker.
    message_text = req.text
    if req.viewpoint_id:
        resolved_viewpoint_id = req.viewpoint_id
    elif req.speaker_type == "character":
        resolved_viewpoint_id = req.speaker_id or req.character_id
    else:
        resolved_viewpoint_id = req.speaker_id

    payload: Dict[str, Any] = dict(req.payload or {})
    payload.update(
        {
            "text": req.text,
            "character_id": req.character_id,
            "user_name": req.user_name,
            "speaker_type": req.speaker_type,
            "speaker_id": req.speaker_id,
            "recipient_id": req.recipient_id,
            "viewpoint_id": resolved_viewpoint_id,
            "pivot_character_id": req.character_id,
            "identity_ruleset": "character-id-v1",
            "scope_key": req.scope_key or settings.default_scope,
        }
    )

    # Python's built-in hash() is process-randomized and therefore unsuitable
    # for persistent dedupe keys. SHA-256 gives stable identity across restarts.
    text_digest = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
    dedupe_key = req.dedupe_key or (
        f"{req.session_id}::{req.kind or 'other'}::{req.speaker_type}::"
        f"{req.speaker_id or ''}::{text_digest}"
    )

    try:
        ev = await db.execute_returning_row(
            """
            INSERT INTO aios.ingest_event (
                event_time,
                source,
                kind,
                session_id,
                speaker_id,
                speaker_role,
                recipient_id,
                viewpoint_id,
                character_id,
                user_name,
                message_text,
                payload,
                dedupe_key
            )
            VALUES (
                now(),
                $1,
                $2::aios.event_kind,
                $3,
                $4,
                $5::aios.actor_type,
                $6,
                $7,
                $8,
                $9,
                $10,
                $11::jsonb,
                $12
            )
            ON CONFLICT (dedupe_key) DO UPDATE
            SET dedupe_key = EXCLUDED.dedupe_key
            RETURNING event_id
            """,
            settings.source_name,
            req.kind or "other",
            req.session_id,
            req.speaker_id,
            req.speaker_type,
            req.recipient_id,
            resolved_viewpoint_id,
            req.character_id,
            req.user_name,
            message_text,
            json.dumps(payload),
            dedupe_key,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Insert ingest_event failed: {exc}",
        ) from exc

    event_id = int(ev["event_id"])

    try:
        # -------------------------------------------------
        # Timeline resolution (STRUCTURAL ONLY)
        # -------------------------------------------------
        timeline_id = await get_or_create_timeline(
            db,
            world_key="liminal",
            session_id=req.session_id,
            character_id=req.character_id,
            user_name=req.user_name,
            scope_key=req.scope_key or settings.default_scope,
            meta={
                "source": settings.source_name,
                "world_assignment": "default_liminal",
            },
        )

        # -------------------------------------------------
        # DAG append
        # -------------------------------------------------
        # add_node_and_edge also flips the durable DAG stage latch on the
        # originating ingest_event.
        node_id, _ = await add_node_and_edge(
            db,
            timeline_id=timeline_id,
            event_id=event_id,
            character_id=req.character_id,
            kind=req.kind or "other",
            speaker_id=req.speaker_id,
            speaker_role=req.speaker_type,
            recipient_id=req.recipient_id,
            message_text=message_text,
            payload=payload,
            viewpoint_id=resolved_viewpoint_id,
            edge_type="next",
        )

        # -------------------------------------------------
        # Runtime source-perception cursor advancement
        # -------------------------------------------------
        # The liminal/source DAG remains immutable provenance.  Matching active
        # runtime instances merely advance their authorized perception boundary;
        # source messages are never copied into the concrete runtime DAG.
        await db.execute(
            """
            UPDATE aios.character_runtime_state rs
            SET source_timeline_id=$1,
                source_head_node_id=$2,
                updated_at=now()
            FROM aios.character_instance ci,
                 aios.timeline rt,
                 aios.world rw
            WHERE ci.instance_id=rs.instance_id
              AND rt.timeline_id=rs.timeline_id
              AND rw.world_id=rs.world_id
              AND ci.character_id=$3
              AND rt.session_id IS NOT DISTINCT FROM $4
              AND rt.user_name IS NOT DISTINCT FROM $5
              AND rt.scope_key=$6
              AND (
                    rs.source_timeline_id=$1
                    OR (
                        rs.source_timeline_id IS NULL
                        AND rw.anchor_timeline_id=$1
                    )
                  )
              AND COALESCE(
                    (
                        SELECT dn.event_id
                        FROM aios.dag_node dn
                        WHERE dn.node_id=rs.source_head_node_id
                    ),
                    -1
                  ) <= $7
            """,
            timeline_id,
            node_id,
            req.character_id,
            req.session_id,
            req.user_name,
            req.scope_key or settings.default_scope,
            event_id,
        )

        await mark_matching_runtime_dirty(
            db,
            character_id=req.character_id,
            session_id=req.session_id,
            user_name=req.user_name,
            scope_key=req.scope_key or settings.default_scope,
            source_timeline_id=timeline_id,
            source_head_node_id=node_id,
            source_head_event_id=event_id,
        )
    except Exception as exc:
        await db.execute(
            """
            UPDATE aios.ingest_event
            SET process_status = 'error',
                process_error = $2,
                processed_at = NULL
            WHERE event_id = $1
            """,
            event_id,
            repr(exc)[:2000],
        )
        raise HTTPException(
            status_code=500,
            detail=f"DAG ingestion failed for event_id={event_id}: {exc}",
        ) from exc

    return IngestOut(
        ok=True,
        event_id=event_id,
        node_id=node_id,
        timeline_id=timeline_id,
    )


@app.post("/observation", response_model=IngestOut)
async def ingest_external_observation(req: ExternalObservationIn) -> IngestOut:
    """
    Persist one non-character observation into the liminal DAG.

    This endpoint is the common ingress for future accumulators and other
    external sensors. Source/speaker provenance is preserved independently
    from optional character/world target hints. Those hints never assert world
    truth and never make the observation part of a character memory.
    """
    try:
        return await persist_external_observation(db, req)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"External observation ingestion failed: {exc}",
        ) from exc


# =================================================
# Memory read (READ-ONLY)
# =================================================

@app.get("/memory", response_model=MemoryOut)
async def memory(
    character: str,
    context: str,
    user: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 8,
) -> MemoryOut:
    timeline_id = await pick_latest_timeline_for_character(
        db,
        character_id=character,
        user_name=user,
        scope_key=scope or settings.default_scope,
    )

    if not timeline_id:
        return MemoryOut(timeline_id=None, vector_matches=[])

    matches = await recent_nodes_as_memory(
        db,
        timeline_id=timeline_id,
        limit=limit,
    )

    return MemoryOut(
        timeline_id=timeline_id,
        vector_matches=matches,
    )


# =================================================
# Shared concrete world runtime
# =================================================

@app.post("/character/{character_id}/activate", response_model=CharacterActivateOut)
async def activate_character(character_id: str, req: CharacterActivateIn) -> CharacterActivateOut:
    """
    Materialize or resume one experiential character instance in a concrete world.

    controller_type distinguishes live humans from LLM agents/scripts without
    changing the world/entity model seen by the simulation.
    """
    try:
        result = await world_runtime.activate_character(
            character_id=character_id,
            user_name=req.user_name,
            session_id=req.session_id,
            scope_key=req.scope_key,
            world_id=req.world_id,
            world_key=req.world_key,
            controller_type=req.controller_type,
            controller_ref=req.controller_ref,
        )
        return CharacterActivateOut(**result.__dict__)
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/instance/{instance_id}/state")
async def get_instance_state(instance_id: UUID):
    try:
        return await world_runtime.get_state(instance_id)
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/instance/{instance_id}/frame")
async def get_instance_frame(
    instance_id: UUID,
    recent_limit: Optional[int] = None,
    token_budget: Optional[int] = None,
    wait_ms: int = 1200,
):
    """Build the canonical branch-aware RPG HUD for this runtime instance."""
    try:
        return await world_runtime.build_frame(
            instance_id,
            recent_limit=recent_limit,
            token_budget=token_budget,
            wait_ms=wait_ms,
        )
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc




@app.post("/instance/{instance_id}/prepare")
async def prepare_instance_frame(
    instance_id: UUID,
    through_node_id: Optional[UUID] = None,
    recent_limit: Optional[int] = None,
    token_budget: Optional[int] = None,
    wait_ms: int = 2500,
):
    """
    Prepare a generation-consistent HUD through an exact source DAG node.

    This advances only latency-critical retrieval work. Background RDF,
    narrative clustering, web accumulation, and unrelated instances are not
    part of the generation barrier.
    """
    try:
        frame = await world_runtime.prepare_frame(
            instance_id,
            through_node_id=through_node_id,
            recent_limit=recent_limit,
            token_budget=token_budget,
            wait_ms=wait_ms,
        )
        return {
            "instance_id": instance_id,
            "generation_ready": bool(frame.get("hud", {}).get("generation_ready")),
            "freshness": frame.get("hud", {}).get("freshness", {}),
            "frame": frame,
            "text": render_hud_text(frame),
        }
    except RuntimeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/instance/{instance_id}/action")
async def apply_instance_action(instance_id: UUID, req: WorldActionIn):
    try:
        return await world_runtime.apply_action(
            instance_id=instance_id,
            expected_state_version=req.expected_state_version,
            action_type=req.action_type,
            target_entity_id=req.target_entity_id,
            text=req.text,
            payload=req.payload,
        )
    except RuntimeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/world/{world_id}/entity")
async def create_world_entity(world_id: UUID, req: WorldEntityCreateIn):
    try:
        return await world_runtime.create_entity(
            world_id=world_id,
            entity_key=req.entity_key,
            entity_type=req.entity_type,
            display_name=req.display_name,
            meta=req.meta,
        )
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/world/{world_id}/relation")
async def create_world_relation(world_id: UUID, req: WorldRelationCreateIn):
    """
    Express composable relations such as hosted_on, wearing, riding,
    attached_to, controlling, inside, or observes_through.
    """
    try:
        return await world_runtime.relate_entities(
            world_id=world_id,
            subject_entity_id=req.subject_entity_id,
            relation_type=req.relation_type,
            object_entity_id=req.object_entity_id,
            meta=req.meta,
        )
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/world/{world_id}/rule/{rule_key}")
async def put_world_rule(world_id: UUID, rule_key: str, req: WorldRulePutIn):
    return await world_runtime.upsert_rule(
        world_id=world_id,
        rule_key=rule_key,
        rule_type=req.rule_type,
        enabled=req.enabled,
        priority=req.priority,
        rule_data=req.rule_data,
    )


@app.post("/instance/{instance_id}/fork", response_model=CharacterActivateOut)
async def fork_instance(instance_id: UUID, req: CharacterForkIn) -> CharacterActivateOut:
    """Create a new experiential continuation in another epistemic/runtime world."""
    try:
        result = await world_runtime.fork_instance(
            source_instance_id=instance_id,
            target_world_id=req.target_world_id,
            target_world_key=req.target_world_key,
        )
        return CharacterActivateOut(**result.__dict__)
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/instance/{instance_id}/frame/text")
async def get_instance_text_frame(
    instance_id: UUID,
    recent_limit: Optional[int] = None,
    token_budget: Optional[int] = None,
    wait_ms: int = 1200,
):
    try:
        frame = await world_runtime.build_frame(
            instance_id,
            recent_limit=recent_limit,
            token_budget=token_budget,
            wait_ms=wait_ms,
        )
        return {
            "instance_id": instance_id,
            "generation_ready": bool(frame.get("hud", {}).get("generation_ready")),
            "freshness": frame.get("hud", {}).get("freshness", {}),
            "text": render_hud_text(frame),
        }
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/entity/{entity_id}/controller")
async def add_entity_controller(entity_id: UUID, req: EntityControllerIn):
    try:
        return await world_runtime.add_controller(
            entity_id=entity_id,
            controller_type=req.controller_type,
            controller_ref=req.controller_ref,
            authority=req.authority,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# =================================================
# Epistemic control API
# =================================================

@app.post("/instance/{instance_id}/knowledge/acquire")
async def acquire_instance_knowledge(instance_id: UUID, req: KnowledgeAcquireIn):
    """
    Record an explicit information-acquisition event.

    This does not grant global /world omniscience. The supervisor projects the
    acquisition into this experiential character instance only.
    """
    try:
        acquisition_id = await record_acquisition(
            db,
            instance_id=instance_id,
            proposition_id=req.proposition_id,
            claim_id=req.claim_id,
            acquisition_mode=req.acquisition_mode,
            epistemic_status=req.epistemic_status,
            confidence=req.confidence,
            source_entity_id=req.source_entity_id,
            dag_node_id=req.dag_node_id,
            meta=req.meta,
        )
        return {
            "ok": True,
            "acquisition_id": acquisition_id,
            "projection_status": "queued",
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/world/{world_id}/fact/generated")
async def generate_provisional_world_fact(world_id: UUID, req: GeneratedFactIn):
    """
    Add a generated gap-fill proposition as provisional, never as silent truth.
    Later concrete observations can corroborate or supersede it.
    """
    try:
        return await create_generated_fact(
            db,
            world_id=world_id,
            subject=req.subject,
            predicate=req.predicate,
            object_value=req.object,
            raw_text=req.raw_text,
            confidence=req.confidence,
            generated_at_node_id=req.generated_at_node_id,
            reason=req.reason,
            meta=req.meta,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/epistemic/proposition/{proposition_id}")
async def get_proposition_context(proposition_id: UUID):
    """Show source narratives, evidence distribution, and explicit conflicts."""
    try:
        return await proposition_context(db, proposition_id=proposition_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/world/{world_id}/epistemic")
async def get_world_epistemic_state(world_id: UUID):
    """Inspect provisional, observed, corroborated, and superseded world facts."""
    try:
        return await world_epistemic_state(db, world_id=world_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/world/{world_id}/fact/observed")
async def import_observed_world_fact(world_id: UUID, req: WorldObservedFactIn):
    """
    Promote an already-normalized RP/web observation into this concrete world.

    This is explicit world bootstrap, not automatic truth promotion from a
    scraped source.
    """
    try:
        assertion_id = await assert_claim_or_proposition_in_world(
            db,
            world_id=world_id,
            proposition_id=req.proposition_id,
            claim_id=req.claim_id,
            confidence=req.confidence,
            reason=req.reason,
        )
        return {"ok": True, "assertion_id": assertion_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# =================================================
# Long-document + deterministic epistemic APIs
# =================================================

@app.post("/document/ingest")
async def ingest_document(req: LongDocumentIn):
    """
    Ingest arbitrary long-form text into its own source document and DAG.

    Metadata is optional and document-derived. No ISBN/DOI/author is required.
    """
    try:
        return await ingest_long_document(
            db,
            text=req.text,
            source_type=req.source_type,
            source_uri=req.source_uri,
            title=req.title,
            source_name=req.source_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/document/{document_id}/epistemic")
async def get_document_epistemic_summary(document_id: UUID):
    try:
        return await document_epistemic_summary(db, document_id=document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/instance/{instance_id}/knowledge/acquire/document/{document_id}")
async def acquire_document_knowledge(
    instance_id: UUID,
    document_id: UUID,
    req: DocumentAcquireIn,
):
    try:
        return await acquire_document(
            db,
            instance_id=instance_id,
            document_id=document_id,
            acquisition_mode=req.acquisition_mode,
            epistemic_status=req.epistemic_status,
            confidence=req.confidence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/character/{character_id}/epistemic-profile")
async def get_character_epistemic_profile(character_id: str):
    try:
        return await get_profile(db, character_id=character_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/character/{character_id}/epistemic-profile")
async def put_character_epistemic_profile(
    character_id: str,
    req: CharacterEpistemicProfileIn,
):
    try:
        profile = await upsert_profile(
            db,
            character_id=character_id,
            data=req.model_dump(),
        )
        reweighted = await reweight_character_knowledge(
            db,
            character_id=character_id,
        )
        return {
            "profile": profile,
            "reweighted_knowledge_rows": reweighted,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/epistemic/search")
async def search_epistemic(req: EpistemicSearchIn):
    return await epistemic_search(
        db,
        query=req.query,
        limit=max(1, min(req.limit, 200)),
        character_id=req.character_id,
        instance_id=req.instance_id,
        source_key=req.source_key,
        include_conflicts=req.include_conflicts,
    )
