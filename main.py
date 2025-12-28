# aios_app/main.py

from __future__ import annotations

import json
import logging
from typing import Optional, Dict, Any

import anyio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import Database
from .models import SessionCreate, SessionOut, IngestIn, IngestOut, MemoryOut
from .dag import get_or_create_timeline, add_node_and_edge
from .memory import recent_nodes_as_memory, pick_latest_timeline_for_character

from .rdf.fuseki import FusekiClient
from .rdf.character_writer import CharacterWriteContext, write_character_event

logger = logging.getLogger("aios.main")

app = FastAPI(title="AIOS MemoryVault", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = Database(settings.db_dsn)

fuseki = FusekiClient(
    settings.fuseki_url,
    timeout=settings.fuseki_timeout,
    retries=settings.fuseki_retries,
)


async def get_or_create_world_id(world_key: str):
    row = await db.fetchrow(
        "SELECT world_id FROM aios.world WHERE world_key = $1",
        world_key,
    )
    if row:
        return row["world_id"]

    created = await db.execute_returning_row(
        """
        INSERT INTO aios.world (world_key, meta)
        VALUES ($1, $2::jsonb)
        RETURNING world_id
        """,
        world_key,
        json.dumps({"type": "auto", "description": "Auto-created world stub"}),
    )
    return created["world_id"]


@app.on_event("startup")
async def startup() -> None:
    await db.connect()


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.close()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/session", response_model=SessionOut)
async def create_session(req: SessionCreate) -> SessionOut:
    source = req.source or settings.source_name

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.session (source, source_session_id, topic, meta)
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING session_id, topic
        """,
        source,
        req.source_session_id,
        req.topic,
        json.dumps(req.meta or {}),
    )
    return SessionOut(session_id=row["session_id"], topic=row["topic"])


@app.post("/ingest", response_model=IngestOut)
async def ingest(req: IngestIn) -> IngestOut:
    scope_key = req.scope_key or settings.default_scope

    message_text = json.dumps(req.text) if isinstance(req.text, dict) else str(req.text)

    payload: Dict[str, Any] = dict(req.payload or {})
    payload.update(
        {
            "text": req.text,
            "character_id": req.character_id,
            "user_name": req.user_name,
            "speaker_type": req.speaker_type,
            "speaker_id": req.speaker_id,
            "recipient_id": req.recipient_id,
            "scope_key": scope_key,
        }
    )

    dedupe_key = req.dedupe_key or f"{req.session_id}::{req.speaker_type}::{hash(message_text)}"

    try:
        ev = await db.execute_returning_row(
            """
            INSERT INTO aios.ingest_event (
                source,
                kind,
                session_id,
                speaker_id,
                speaker_role,
                recipient_id,
                character_id,
                user_name,
                message_text,
                payload,
                dedupe_key
            )
            VALUES (
                $1,
                $2::aios.event_kind,
                $3,
                $4,
                $5::aios.actor_type,
                $6,
                $7,
                $8,
                $9,
                $10::jsonb,
                $11
            )
            RETURNING event_id
            """,
            settings.source_name,
            req.kind or "other",
            req.session_id,
            req.speaker_id,
            req.speaker_type,
            req.recipient_id,
            req.character_id,
            req.user_name,
            message_text,
            json.dumps(payload),
            dedupe_key,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Insert ingest_event failed: {e}")

    event_id = int(ev["event_id"])

    # World resolution
    world_key = payload.get("world_key") or settings.default_world_key
    try:
        world_id = await get_or_create_world_id(world_key)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"World resolution failed: {e}")

    # Timeline (conflict-safe; name derived from character/user/scope)
    timeline_id = await get_or_create_timeline(
        db,
        world_id=world_id,
        session_id=req.session_id,
        character_id=req.character_id,
        user_name=req.user_name,
        scope_key=scope_key,
        meta={"source": settings.source_name, "world_key": world_key},
    )

    # DAG node
    node_id, _ = await add_node_and_edge(
        db,
        timeline_id=timeline_id,
        event_id=event_id,
        kind=req.kind or "other",
        speaker_id=req.speaker_id,
        speaker_role=req.speaker_type,
        recipient_id=req.recipient_id,
        message_text=message_text,
        payload=payload,
        edge_type="next",
    )

    # Mark processed
    await db.execute(
        """
        UPDATE aios.ingest_event
        SET process_status = 'done',
            processed_at = now(),
            process_error = NULL
        WHERE event_id = $1
        """,
        event_id,
    )

    # RDF write (non-fatal) – run in worker thread so we never block the loop
    ctx = CharacterWriteContext(
        dataset=settings.fuseki_character_dataset,
        character_id=req.character_id,
        world_id=world_id,
        timeline_id=timeline_id,
        node_id=node_id,
        event_id=event_id,
        speaker_type=req.speaker_type,
        speaker_id=req.speaker_id,
        recipient_id=req.recipient_id,
        message_text=message_text,
        payload=payload,
    )

    try:
        ok = await anyio.to_thread.run_sync(write_character_event, fuseki, ctx)
        if not ok:
            logger.warning("RDF write failed (non-fatal); janitor can repair later")
    except Exception:
        logger.warning("RDF write raised unexpectedly (non-fatal)", exc_info=True)

    return IngestOut(ok=True, event_id=event_id, node_id=node_id, timeline_id=timeline_id)


@app.get("/memory", response_model=MemoryOut)
async def memory(
    character: str,
    context: str,
    user: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 8,
) -> MemoryOut:
    scope_key = scope or settings.default_scope

    timeline_id = await pick_latest_timeline_for_character(
        db,
        character_id=character,
        user_name=user,
        scope_key=scope_key,
    )

    if not timeline_id:
        return MemoryOut(timeline_id=None, vector_matches=[])

    matches = await recent_nodes_as_memory(db, timeline_id=timeline_id, limit=limit)
    return MemoryOut(timeline_id=timeline_id, vector_matches=matches)
