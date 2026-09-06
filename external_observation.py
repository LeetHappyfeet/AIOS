from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse
from uuid import UUID

from aios_app.dag import add_node_and_edge, get_or_create_timeline
from aios_app.db import Database
from aios_app.models import ExternalObservationIn, IngestOut

PROVENANCE_VERSION = "provenance-v1"
LIMINAL_WORLD_KEY = "liminal"


def _canonical_domain(uri: str | None) -> str | None:
    if not uri:
        return None
    try:
        return urlparse(uri).netloc.lower() or None
    except ValueError:
        return None


async def ensure_source_identity(db: Database, req: ExternalObservationIn) -> None:
    """Create or enrich a durable source identity without changing its type."""
    existing = await db.fetchrow(
        "SELECT source_kind FROM aios.source_identity WHERE source_id=$1",
        req.source_id,
    )
    if existing and existing["source_kind"] != req.source_kind:
        raise ValueError(
            f"source_id {req.source_id!r} is already registered as "
            f"{existing['source_kind']!r}, not {req.source_kind!r}"
        )

    await db.execute(
        """
        INSERT INTO aios.source_identity (
            source_id, source_kind, display_name, canonical_uri,
            canonical_domain, meta
        )
        VALUES ($1,$2,$3,$4,$5,$6::jsonb)
        ON CONFLICT (source_id) DO UPDATE
        SET display_name=COALESCE(aios.source_identity.display_name, EXCLUDED.display_name),
            canonical_uri=COALESCE(aios.source_identity.canonical_uri, EXCLUDED.canonical_uri),
            canonical_domain=COALESCE(aios.source_identity.canonical_domain, EXCLUDED.canonical_domain),
            meta=aios.source_identity.meta || EXCLUDED.meta,
            updated_at=now()
        """,
        req.source_id,
        req.source_kind,
        req.source_name,
        req.source_uri,
        _canonical_domain(req.source_uri),
        json.dumps(req.source_meta or {}),
    )


async def persist_external_observation(
    db: Database,
    req: ExternalObservationIn,
) -> IngestOut:
    """
    Persist one source observation into the liminal temporal DAG.

    Boundary rules:
    - source_id identifies provenance, never a character.
    - speaker_id identifies an asserting source/author when known.
    - target_character_id and target_world_id are downstream intent only.
    - external observations never set origin character ownership.
    - concrete world assertion and character acquisition happen later.
    """
    await ensure_source_identity(db, req)

    payload = dict(req.payload or {})
    payload.update(
        {
            "text": req.text,
            "source_id": req.source_id,
            "source_kind": req.source_kind,
            "source_uri": req.source_uri,
            "speaker_id": req.speaker_id,
            "speaker_type": req.speaker_type,
            "recipient_id": req.recipient_id,
            "viewpoint_id": req.viewpoint_id,
            "target_character_id": req.target_character_id,
            "target_world_id": str(req.target_world_id) if req.target_world_id else None,
            "provenance_version": PROVENANCE_VERSION,
            "identity_ruleset": "external-source-v1",
        }
    )

    text_digest = hashlib.sha256(req.text.encode("utf-8")).hexdigest()
    source_event_id = req.source_event_id or text_digest
    dedupe_key = req.dedupe_key or (
        f"external::{req.source_id}::{source_event_id}::{req.kind}"
    )

    event = await db.execute_returning_row(
        """
        INSERT INTO aios.ingest_event (
            event_time, source, source_id, source_kind, source_event_id,
            kind, session_id, speaker_id, speaker_role, recipient_id,
            viewpoint_id, character_id, user_name, message_text, payload,
            dedupe_key, target_character_id, target_world_id,
            provenance_version
        )
        VALUES (
            COALESCE($1, now()), $2, $3, $4, $5,
            $6::aios.event_kind, $7, $8, $9::aios.actor_type, $10,
            $11, NULL, NULL, $12, $13::jsonb,
            $14, $15, $16, $17
        )
        ON CONFLICT (dedupe_key) DO UPDATE
        SET dedupe_key=EXCLUDED.dedupe_key
        RETURNING event_id
        """,
        req.event_time,
        req.source_id,
        req.source_id,
        req.source_kind,
        source_event_id,
        req.kind,
        req.session_id,
        req.speaker_id,
        req.speaker_type,
        req.recipient_id,
        req.viewpoint_id,
        req.text,
        json.dumps(payload),
        dedupe_key,
        req.target_character_id,
        req.target_world_id,
        PROVENANCE_VERSION,
    )
    event_id = int(event["event_id"])

    scope_key = req.scope_key or f"source:{req.source_id}"
    timeline_id = await get_or_create_timeline(
        db,
        world_key=LIMINAL_WORLD_KEY,
        session_id=req.session_id,
        character_id=None,
        user_name=None,
        scope_key=scope_key,
        meta={
            "source_id": req.source_id,
            "source_kind": req.source_kind,
            "source_uri": req.source_uri,
            "world_assignment": "default_liminal",
            "target_character_id": req.target_character_id,
            "target_world_id": str(req.target_world_id) if req.target_world_id else None,
            "provenance_version": PROVENANCE_VERSION,
        },
        source_id=req.source_id,
    )

    node_id, _ = await add_node_and_edge(
        db,
        timeline_id=timeline_id,
        event_id=event_id,
        character_id=None,
        kind=req.kind,
        speaker_id=req.speaker_id,
        speaker_role=req.speaker_type,
        recipient_id=req.recipient_id,
        message_text=req.text,
        payload=payload,
        viewpoint_id=req.viewpoint_id,
        edge_type="next",
    )

    return IngestOut(
        ok=True,
        event_id=event_id,
        node_id=node_id,
        timeline_id=timeline_id,
    )
