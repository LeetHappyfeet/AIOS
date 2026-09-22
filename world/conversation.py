from __future__ import annotations

import json
from typing import Iterable
from uuid import UUID

from aios_app.db import Database


async def resolve_character_identity(db: Database, source_actor_id: str) -> str | None:
    """Resolve an imported actor without fuzzy/name-substring merging."""
    row = await db.fetchrow(
        """SELECT character_id FROM aios.character_identity
           WHERE lower(character_id)=lower($1)
              OR lower(COALESCE(display_name,''))=lower($1)
           ORDER BY CASE WHEN lower(character_id)=lower($1) THEN 0 ELSE 1 END
           LIMIT 1""",
        source_actor_id,
    )
    return str(row["character_id"]) if row else None


async def ensure_participant(
    db: Database, *, timeline_id: UUID, source_actor_id: str,
    actor_type: str, controller_type: str | None = None,
    controller_ref: str | None = None, participant_role: str = "participant",
    character_id: str | None = None, meta: dict | None = None,
) -> UUID:
    character_id = character_id or await resolve_character_identity(db, source_actor_id)
    row = await db.execute_returning_row(
        """INSERT INTO aios.conversation_participant(
             timeline_id,character_id,source_actor_id,actor_type,controller_type,
             controller_ref,participant_role,meta)
           VALUES($1,$2,$3,$4::aios.actor_type,$5,$6,$7,$8::jsonb)
           ON CONFLICT (timeline_id,source_actor_id) DO UPDATE SET
             character_id=COALESCE(aios.conversation_participant.character_id,EXCLUDED.character_id),
             actor_type=EXCLUDED.actor_type,
             controller_type=COALESCE(aios.conversation_participant.controller_type,EXCLUDED.controller_type),
             controller_ref=COALESCE(aios.conversation_participant.controller_ref,EXCLUDED.controller_ref),
             active=true,updated_at=now(),
             meta=aios.conversation_participant.meta||EXCLUDED.meta
           RETURNING participant_id""",
        timeline_id, character_id, source_actor_id, actor_type, controller_type,
        controller_ref, participant_role, json.dumps(meta or {}),
    )
    return row["participant_id"]


async def bind_available_instance(db: Database, *, participant_id: UUID) -> UUID | None:
    """Bind a participant to an existing runtime; never create a heavyweight runtime."""
    row = await db.execute_returning_row(
        """UPDATE aios.conversation_participant cp SET
             character_instance_id=chosen.instance_id,updated_at=now()
           FROM LATERAL (
             SELECT ci.instance_id FROM aios.character_instance ci
             WHERE ci.character_id=cp.character_id
             ORDER BY ci.created_at DESC LIMIT 1
           ) chosen
           WHERE cp.participant_id=$1 AND cp.character_id IS NOT NULL
           RETURNING cp.character_instance_id""",
        participant_id,
    )
    return row["character_instance_id"] if row else None


async def record_message_participation(
    db: Database, *, timeline_id: UUID, node_id: UUID, speaker_id: str | None,
    addressee_ids: Iterable[str] = (), audience_ids: Iterable[str] = (),
) -> None:
    participants = await db.fetch(
        """SELECT participant_id,source_actor_id FROM aios.conversation_participant
           WHERE timeline_id=$1 AND active""", timeline_id
    )
    by_actor = {str(r["source_actor_id"]): r["participant_id"] for r in participants}
    speaker_pid = by_actor.get(speaker_id or "")
    if speaker_pid:
        await _record_relation(db,node_id,speaker_pid,"speaker")
    addressees = {a for a in addressee_ids if a}
    for actor in addressees:
        pid=by_actor.get(actor)
        if pid: await _record_relation(db,node_id,pid,"addressee")
    explicit = ({speaker_id} if speaker_id else set()) | addressees
    requested_audience = set(audience_ids)
    for actor,pid in by_actor.items():
        if actor in explicit: continue
        if requested_audience and actor not in requested_audience: continue
        await _record_relation(db,node_id,pid,"audience")


async def _record_relation(db: Database,node_id: UUID,participant_id: UUID,relation: str) -> None:
    await db.execute(
        """INSERT INTO aios.message_participant(node_id,participant_id,relation,perceived)
           VALUES($1,$2,$3,true) ON CONFLICT DO NOTHING""",
        node_id,participant_id,relation,
    )


async def perceiving_instances(db: Database, *, node_id: UUID) -> list[UUID]:
    rows=await db.fetch(
        """SELECT DISTINCT cp.character_instance_id AS instance_id
           FROM aios.message_participant mp
           JOIN aios.conversation_participant cp ON cp.participant_id=mp.participant_id
           WHERE mp.node_id=$1 AND mp.perceived AND cp.active
             AND cp.character_instance_id IS NOT NULL""", node_id
    )
    return [r["instance_id"] for r in rows]
