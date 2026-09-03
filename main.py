from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from aios_app.config import settings
from aios_app.db import Database
from aios_app.models import (
    SessionCreate,
    SessionOut,
    IngestIn,
    IngestOut,
    MemoryOut,
)
from aios_app.dag import get_or_create_timeline, add_node_and_edge
from aios_app.memory import recent_nodes_as_memory, pick_latest_timeline_for_character

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

# =================================================
# Lifecycle
# =================================================

@app.on_event("startup")
async def startup() -> None:
    await db.connect()
    logger.info("Database connected")


@app.on_event("shutdown")
async def shutdown() -> None:
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

    # IngestIn.text is intentionally canonical linguistic content. Structured
    # transport/application metadata belongs in payload, not message_text.
    message_text = req.text

    payload: Dict[str, Any] = dict(req.payload or {})
    payload.update(
        {
            "text": req.text,
            "character_id": req.character_id,
            "user_name": req.user_name,
            "speaker_type": req.speaker_type,
            "speaker_id": req.speaker_id,
            "recipient_id": req.recipient_id,
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
                $10::jsonb,
                $11
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
            kind=req.kind or "other",
            speaker_id=req.speaker_id,
            speaker_role=req.speaker_type,
            recipient_id=req.recipient_id,
            message_text=message_text,
            payload=payload,
            edge_type="next",
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
