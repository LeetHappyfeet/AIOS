"""Bounded, read-only receipt validation for incremental chat clients."""
from __future__ import annotations

from typing import Literal
from uuid import UUID
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aios_app.config import settings


class SyncReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int = Field(ge=0, le=2147483647)
    event_id: int = Field(gt=0, le=9223372036854775807)
    node_id: UUID
    speaker_type: Literal["user", "character"]
    speaker_id: str = Field(min_length=1, max_length=512)
    text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    message_uuid: str | None = Field(default=None, max_length=128)
    swipe_id: int | None = Field(default=None, ge=0, le=2147483647)


class IngestSyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    character_id: str = Field(min_length=1, max_length=512)
    user_name: str = Field(min_length=1, max_length=512)
    scope_key: str | None = Field(default=None, max_length=512)
    source_head_node_id: UUID | None = None
    receipts: list[SyncReceipt] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def unique_slots(self):
        slots = [r.message_id for r in self.receipts]
        if len(set(slots)) != len(slots):
            raise ValueError("receipt message_id slots must be unique within a page")
        return self


# One SELECT snapshot validates the binding and all supplied receipts. Walking
# at most 4097 distinct nodes bounds even cyclic/multi-parent source DAGs. Older
# nodes outside that window are conservatively unvalidated, never assumed valid.
SYNC_SQL = """
WITH RECURSIVE runtime AS (
    SELECT rs.instance_id, rs.source_timeline_id, rs.source_head_node_id,
           rs.state_version,
           (ci.character_id=$3 AND rt.session_id=$2
            AND rt.user_name=$4 AND rt.scope_key=$5
            AND (rs.source_timeline_id IS NULL OR
                 (st.session_id=$2 AND st.scope_key=$5))) AS mapping_matches
    FROM aios.character_runtime_state rs
    JOIN aios.character_instance ci USING(instance_id)
    JOIN aios.timeline rt ON rt.timeline_id=rs.timeline_id
    LEFT JOIN aios.timeline st ON st.timeline_id=rs.source_timeline_id
    WHERE rs.instance_id=$1
), walk(node_id) AS (
    SELECT h.node_id FROM runtime r JOIN aios.dag_node h
        ON h.node_id=r.source_head_node_id AND h.timeline_id=r.source_timeline_id
        WHERE r.mapping_matches
    UNION
    SELECT e.parent_node_id FROM walk w
    JOIN aios.dag_edge e ON e.child_node_id=w.node_id
    JOIN aios.dag_node p ON p.node_id=e.parent_node_id
    JOIN runtime r ON p.timeline_id=r.source_timeline_id AND e.timeline_id=r.source_timeline_id
), active_branch AS MATERIALIZED (
    SELECT node_id FROM walk LIMIT 4097
), requested AS (
    SELECT item FROM jsonb_array_elements($6::jsonb) item
)
SELECT r.*, q.item->>'message_id' AS message_id,
       (SELECT count(*)>4096 FROM active_branch) AS branch_window_exhausted,
       COALESCE(ie.event_id IS NOT NULL AND dn.node_id IS NOT NULL
           AND ie.superseded_at IS NULL
           AND ie.process_status IS DISTINCT FROM 'error'
           AND ie.process_error IS DISTINCT FROM 'participant_binding_pending'
           AND ie.speaker_id=q.item->>'speaker_id'
           AND ie.speaker_role::text=q.item->>'speaker_type'
           AND ie.source_event_id=(q.item->>'speaker_type') || ':' || (q.item->>'message_id')
           AND encode(sha256(convert_to(ie.message_text,'UTF8')),'hex')=q.item->>'text_hash'
           AND ie.payload->>'client_message_id' IS NOT DISTINCT FROM q.item->>'message_uuid'
           AND ie.payload->>'swipe_id' IS NOT DISTINCT FROM q.item->>'swipe_id'
           AND EXISTS (SELECT 1 FROM active_branch b WHERE b.node_id=dn.node_id), false) AS valid
FROM runtime r LEFT JOIN requested q ON true
LEFT JOIN aios.ingest_event ie ON r.mapping_matches
    AND ie.event_id=(q.item->>'event_id')::bigint
    AND ie.session_id=$2 AND ie.character_id=$3 AND ie.user_name=$4
    AND COALESCE(ie.payload->>'scope_key',$5)=$5 AND lower(ie.source)='sillytavern'
LEFT JOIN aios.dag_node dn ON dn.node_id=(q.item->>'node_id')::uuid
    AND dn.event_id=ie.event_id AND dn.timeline_id=r.source_timeline_id
ORDER BY (q.item->>'message_id')::bigint
"""


async def validate_ingest_receipts(db, instance_id: UUID, req: IngestSyncIn):
    try:
        rows = await db.fetch_bounded(
            SYNC_SQL, instance_id, req.session_id, req.character_id, req.user_name,
            req.scope_key or settings.default_scope,
            json.dumps([r.model_dump(mode="json") for r in req.receipts]),
            timeout_seconds=2.0,
        )
    except Exception as exc:
        if isinstance(exc, TimeoutError) or getattr(exc, "sqlstate", None) in {"57014", "55P03"}:
            raise HTTPException(503, "Receipt validation timed out; do not trust unchecked receipts") from exc
        raise
    if not rows:
        raise HTTPException(404, "Unknown runtime instance")
    state = rows[0]
    if not state["mapping_matches"]:
        raise HTTPException(409, "Session/character/user/scope does not match this runtime")
    receipts = [{"message_id": int(r["message_id"]), "valid": bool(r["valid"]),
                 "reason": "matched" if r["valid"] else "missing_stale_or_off_branch"}
                for r in rows if r["message_id"] is not None]
    head_matches = req.source_head_node_id == state["source_head_node_id"]
    return {
        "protocol": "ingest-sync-v1", "instance_id": instance_id,
        "session_id": req.session_id, "source_timeline_id": state["source_timeline_id"],
        "source_head_node_id": state["source_head_node_id"], "state_version": state["state_version"],
        "head_matches": head_matches, "receipts": receipts,
        "replay_required": not head_matches or any(not r["valid"] for r in receipts),
        "branch_window_exhausted": bool(state["branch_window_exhausted"]),
        "checked_count": len(receipts), "max_receipts": 256,
        "complete_transcript_verified": False,
    }
