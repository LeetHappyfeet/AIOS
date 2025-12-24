from fastapi import FastAPI, HTTPException
from typing import Optional, Dict, Any
from fastapi.middleware.cors import CORSMiddleware
import json

from .config import settings
from .db import Database
from .models import (
    SessionCreate, SessionOut,
    IngestIn, IngestOut,
    MemoryOut,
)
from .dag import get_or_create_timeline, add_node_and_edge
from .memory import recent_nodes_as_memory, pick_latest_timeline_for_character

app = FastAPI(title="AIOS MemoryVault", version="0.1.0")

# -------------------------------------------------
# CORS (browser + SillyTavern safe)
# -------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = Database(settings.db_dsn)

# -------------------------------------------------
# Lifecycle
# -------------------------------------------------
@app.on_event("startup")
async def startup() -> None:
    await db.connect()

@app.on_event("shutdown")
async def shutdown() -> None:
    await db.close()

# -------------------------------------------------
# Health
# -------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True}

# -------------------------------------------------
# Session
# -------------------------------------------------
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

    return SessionOut(
        session_id=row["session_id"],
        topic=row["topic"],
    )

# -------------------------------------------------
# Ingest
# -------------------------------------------------
@app.post("/ingest", response_model=IngestOut)
async def ingest(req: IngestIn) -> IngestOut:
    scope_key = req.scope_key or settings.default_scope

    # ✅ Normalize message text (CRITICAL FIX)
    if isinstance(req.text, dict):
        message_text = json.dumps(req.text)
    else:
        message_text = str(req.text)

    # Build payload (raw, lossless)
    payload: Dict[str, Any] = dict(req.payload or {})
    payload.update({
        "text": req.text,
        "character_id": req.character_id,
        "user_name": req.user_name,
        "speaker_type": req.speaker_type,
        "speaker_id": req.speaker_id,
        "recipient_id": req.recipient_id,
        "scope_key": scope_key,
    })

    # Dedupe key
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
            message_text,                 # ✅ string
            json.dumps(payload),          # ✅ jsonb
            dedupe_key,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Insert ingest_event failed: {e}",
        )

    event_id = int(ev["event_id"])

    # Timeline
    timeline_id = await get_or_create_timeline(
        db,
        session_id=req.session_id,
        character_id=req.character_id,
        user_name=req.user_name,
        scope_key=scope_key,
        meta={"source": settings.source_name},
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

    return IngestOut(
        ok=True,
        event_id=event_id,
        node_id=node_id,
        timeline_id=timeline_id,
    )

# -------------------------------------------------
# Memory
# -------------------------------------------------
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

    matches = await recent_nodes_as_memory(
        db,
        timeline_id=timeline_id,
        limit=limit,
    )

    return MemoryOut(
        timeline_id=timeline_id,
        vector_matches=matches,
    )
