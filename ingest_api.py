from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from fastapi import HTTPException

from aios_app.config import settings
from aios_app.dag import add_node_and_edge, get_or_create_timeline
from aios_app.hud.readiness import mark_matching_runtime_dirty
from aios_app.ingest_identity import (
    IngestEventDisposition,
    classify_ingest_event,
    should_short_circuit_replay,
)
from aios_app.models import IngestIn, IngestOut
from aios_app.world.conversation import (
    bind_available_instance,
    ensure_participant,
    record_message_participation,
)


async def _runtime_source_head(db, req: IngestIn, timeline_id):
    row = await db.fetchrow(
        """
        SELECT rs.source_head_node_id
        FROM aios.character_runtime_state rs
        JOIN aios.character_instance ci ON ci.instance_id=rs.instance_id
        JOIN aios.timeline rt ON rt.timeline_id=rs.timeline_id
        WHERE ci.character_id=$1
          AND rt.session_id IS NOT DISTINCT FROM $2
          AND rt.user_name IS NOT DISTINCT FROM $3
          AND rt.scope_key=$4
          AND rs.source_timeline_id=$5
        ORDER BY rs.updated_at DESC, rs.instance_id
        LIMIT 1
        """,
        req.character_id,
        req.session_id,
        req.user_name,
        req.scope_key or settings.default_scope,
        timeline_id,
    )
    return row["source_head_node_id"] if row else None


def _ingest_out(*, event_id, node_id, timeline_id, disposition, source_head_node_id):
    return IngestOut(
        ok=True,
        event_id=event_id,
        node_id=node_id,
        timeline_id=timeline_id,
        disposition=disposition.value,
        source_head_node_id=source_head_node_id,
        source_current=bool(source_head_node_id == node_id),
    )


async def ingest_message(db, req: IngestIn) -> IngestOut:
    """Persist one chat message with end-to-end replay idempotency."""
    message_text = req.text
    if req.viewpoint_id:
        resolved_viewpoint_id = req.viewpoint_id
    elif req.speaker_type == "character":
        resolved_viewpoint_id = req.speaker_id or req.character_id
    else:
        resolved_viewpoint_id = req.speaker_id

    payload: Dict[str, Any] = dict(req.payload or {})
    client_source = str(payload.get("source") or settings.source_name).strip() or settings.source_name
    source_message_id = payload.get("message_id")
    source_event_id = (
        f"{req.speaker_type}:{source_message_id}"
        if client_source.lower() == "sillytavern" and source_message_id is not None
        else None
    )
    source_kind = "sillytavern_chat" if source_event_id else None

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

    text_digest = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
    if source_event_id:
        default_dedupe_key = (
            f"{req.session_id}::{client_source.lower()}::{source_event_id}::{text_digest}"
        )
    else:
        default_dedupe_key = (
            f"{req.session_id}::{req.kind or 'other'}::{req.speaker_type}::"
            f"{req.speaker_id or ''}::{text_digest}"
        )
    dedupe_key = req.dedupe_key or default_dedupe_key

    try:
        ev = await db.execute_returning_row(
            """
            INSERT INTO aios.ingest_event (
                event_time, source, source_kind, source_event_id, kind,
                session_id, speaker_id, speaker_role, recipient_id, viewpoint_id,
                character_id, user_name, message_text, payload, dedupe_key
            )
            VALUES (
                now(), $1, $2, $3, $4::aios.event_kind,
                $5, $6, $7::aios.actor_type, $8, $9,
                $10, $11, $12, $13::jsonb, $14
            )
            ON CONFLICT (dedupe_key) DO UPDATE
            SET dedupe_key = EXCLUDED.dedupe_key
            RETURNING event_id,
                      (xmax = 0) AS inserted,
                      (superseded_at IS NOT NULL) AS was_superseded
            """,
            client_source,
            source_kind,
            source_event_id,
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
        raise HTTPException(status_code=400, detail=f"Insert ingest_event failed: {exc}") from exc

    event_id = int(ev["event_id"])
    disposition = classify_ingest_event(
        inserted=bool(ev["inserted"]),
        was_superseded=bool(ev["was_superseded"]),
    )

    # An exact active replay that already reached the DAG is completely durable.
    # Return its original coordinates before any source-slot, cursor, HUD-dirty,
    # or downstream structural work can be repeated. The response separately
    # exposes the actual runtime source head so clients do not mistake a reused
    # historical node for the current generation boundary.
    existing_node = None
    if disposition is IngestEventDisposition.ACTIVE_REPLAY:
        existing_node = await db.fetchrow(
            """
            SELECT node_id, timeline_id
            FROM aios.dag_node
            WHERE event_id=$1
            ORDER BY created_at, node_id
            LIMIT 1
            """,
            event_id,
        )
        if should_short_circuit_replay(disposition, has_dag_node=bool(existing_node)):
            source_head_node_id = await _runtime_source_head(db, req, existing_node["timeline_id"])
            return _ingest_out(
                event_id=event_id,
                node_id=existing_node["node_id"],
                timeline_id=existing_node["timeline_id"],
                disposition=disposition,
                source_head_node_id=source_head_node_id,
            )

    # Only a genuine re-selection of a previously superseded swipe is
    # reactivated. Active replays never mutate their immutable event row.
    if source_event_id and disposition is IngestEventDisposition.SUPERSEDED_RESELECTION:
        await db.execute(
            """
            UPDATE aios.ingest_event
            SET superseded_at=NULL,
                superseded_by_event_id=NULL
            WHERE event_id=$1
            """,
            event_id,
        )

    try:
        timeline_id = await get_or_create_timeline(
            db,
            world_key="liminal",
            session_id=req.session_id,
            character_id=req.character_id,
            user_name=req.user_name,
            scope_key=req.scope_key or settings.default_scope,
            meta={"source": client_source, "world_assignment": "default_liminal"},
        )

        replacement_parent_node_id = None
        source_slot_replaced = False
        if source_event_id:
            prior = await db.fetchrow(
                """
                SELECT ie.event_id, dn.node_id, de.parent_node_id
                FROM aios.ingest_event ie
                LEFT JOIN aios.dag_node dn ON dn.event_id=ie.event_id
                LEFT JOIN LATERAL (
                    SELECT parent_node_id
                    FROM aios.dag_edge
                    WHERE child_node_id=dn.node_id
                    ORDER BY created_at
                    LIMIT 1
                ) de ON true
                WHERE ie.session_id=$1
                  AND lower(ie.source)=lower($2)
                  AND ie.source_event_id=$3
                  AND ie.event_id<>$4
                  AND ie.superseded_at IS NULL
                ORDER BY ie.event_id DESC
                LIMIT 1
                """,
                req.session_id,
                client_source,
                source_event_id,
                event_id,
            )
            if prior:
                source_slot_replaced = True
                replacement_parent_node_id = prior["parent_node_id"]
                await db.execute(
                    """
                    UPDATE aios.ingest_event
                    SET superseded_at=now(), superseded_by_event_id=$2
                    WHERE event_id=$1 AND superseded_at IS NULL
                    """,
                    prior["event_id"],
                    event_id,
                )
                payload["supersedes_event_id"] = int(prior["event_id"])
                payload["source_branch_mode"] = "replacement"

        # Establish all known actors as participants before the node is
        # projected. This keeps live API ingestion on the same multi-party
        # contract as imported chat logs.
        actor_specs: dict[str, tuple[str, str]] = {}
        if req.character_id:
            actor_specs[req.character_id] = ("character", "agent")
        if req.user_name:
            actor_specs.setdefault(req.user_name, ("user", "human"))
        if req.speaker_id:
            actor_specs[req.speaker_id] = (
                req.speaker_type or "character",
                "human" if req.speaker_type == "user" else "agent",
            )
        if req.recipient_id:
            actor_specs.setdefault(req.recipient_id, ("character", "agent"))
        for actor_id, (actor_type, controller_type) in actor_specs.items():
            participant_id = await ensure_participant(
                db,
                timeline_id=timeline_id,
                source_actor_id=actor_id,
                actor_type=actor_type,
                controller_type=controller_type,
                controller_ref=actor_id if controller_type == "human" else f"character:{actor_id}",
                participant_role="primary" if actor_id == req.character_id else "participant",
                meta={"source": client_source, "live_ingest": True},
            )
            await bind_available_instance(db, participant_id=participant_id)

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
            parent_node_id=replacement_parent_node_id,
            edge_type="alternative" if replacement_parent_node_id else "next",
        )

        await record_message_participation(
            db,
            timeline_id=timeline_id,
            node_id=node_id,
            speaker_id=req.speaker_id,
            addressee_ids=[req.recipient_id] if req.recipient_id else [],
            audience_ids=actor_specs.keys(),
        )

        # Source perception has one authority: the cursor propagation layer.
        # It returns the exact runtimes that adopted this event so downstream
        # consequences never rediscover an arbitrary matching instance.
        affected_instance_ids = await mark_matching_runtime_dirty(
            db,
            character_id=req.character_id,
            session_id=req.session_id,
            user_name=req.user_name,
            scope_key=req.scope_key or settings.default_scope,
            source_timeline_id=timeline_id,
            source_head_node_id=node_id,
            source_head_event_id=event_id,
        )
        source_head_node_id = await _runtime_source_head(db, req, timeline_id)

        # A genuinely new perceived host experience contributes one cognitive
        # delta to every runtime that actually adopted the source coordinate.
        # Transport provenance (SillyTavern, API, email, etc.) is not cognition
        # policy. Replays and superseded re-selections are deliberately excluded.
        if disposition is IngestEventDisposition.NEW and affected_instance_ids:
            from aios_app.agent.admission import AutonomyAdmissionService

            admission = AutonomyAdmissionService(db)
            for instance_id in affected_instance_ids:
                await admission.observe_host_experience(
                    instance_id=instance_id,
                    source_node_id=node_id,
                    source_event_id=event_id,
                    source=client_source,
                )
    except Exception as exc:
        await db.execute(
            """
            UPDATE aios.ingest_event
            SET process_status='error', process_error=$2, processed_at=NULL
            WHERE event_id=$1
            """,
            event_id,
            repr(exc)[:2000],
        )
        raise HTTPException(
            status_code=500,
            detail=f"DAG ingestion failed for event_id={event_id}: {exc}",
        ) from exc

    return _ingest_out(
        event_id=event_id,
        node_id=node_id,
        timeline_id=timeline_id,
        disposition=disposition,
        source_head_node_id=source_head_node_id,
    )
