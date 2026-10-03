from __future__ import annotations

import json
from typing import Iterable
from uuid import UUID

from aios_app.db import Database
from aios_app.world.topology import ensure_character_root_world


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
    # Human users are conversation actors, not automatically cognitive characters.
    # A display-name collision must never grant a human a character's instance.
    if actor_type == "user":
        character_id = None
    elif character_id is None:
        character_id = await resolve_character_identity(db, source_actor_id)
    row = await db.execute_returning_row(
        """INSERT INTO aios.conversation_participant(
             timeline_id,character_id,source_actor_id,actor_type,controller_type,
             controller_ref,participant_role,meta)
           VALUES($1,$2,$3,$4::aios.actor_type,$5,$6,$7,$8::jsonb)
           ON CONFLICT (timeline_id,source_actor_id) DO UPDATE SET
             character_id=CASE WHEN EXCLUDED.actor_type='user'::aios.actor_type
               THEN NULL ELSE COALESCE(aios.conversation_participant.character_id,EXCLUDED.character_id) END,
             character_instance_id=CASE WHEN EXCLUDED.actor_type='user'::aios.actor_type
               THEN NULL ELSE aios.conversation_participant.character_instance_id END,
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
    """Preserve an existing binding; resolve new bindings within the conversation."""
    # PostgreSQL does not make the UPDATE target alias visible inside a
    # FROM/LATERAL item at this query level. Use a correlated scalar subquery,
    # which is explicitly allowed to read the target row's old values.
    row = await db.execute_returning_row(
        """UPDATE aios.conversation_participant cp SET
             character_instance_id=(
               SELECT ci.instance_id
               FROM aios.character_instance ci
               JOIN aios.character_runtime_state rs ON rs.instance_id=ci.instance_id
               JOIN aios.timeline rt ON rt.timeline_id=rs.timeline_id
               JOIN aios.timeline st ON st.timeline_id=cp.timeline_id
               WHERE ci.character_id=cp.character_id
                 AND rt.session_id IS NOT DISTINCT FROM st.session_id
                 AND rt.user_name IS NOT DISTINCT FROM st.user_name
                 AND rt.scope_key=st.scope_key
                 AND (rs.source_timeline_id IS NULL OR rs.source_timeline_id=cp.timeline_id)
               ORDER BY ci.created_at DESC
               LIMIT 1
             ),
             updated_at=now()
           WHERE cp.participant_id=$1
             AND cp.actor_type='character'::aios.actor_type
             AND cp.character_id IS NOT NULL
             AND cp.character_instance_id IS NULL
             AND EXISTS (
               SELECT 1 FROM aios.character_instance ci
               WHERE ci.character_id=cp.character_id
             )
           RETURNING cp.character_instance_id""",
        participant_id,
    )
    return row["character_instance_id"] if row else None


async def reconcile_runtime_observations(db: Database, *, instance_id: UUID) -> int:
    """Recover bounded missing deliveries within this runtime's adopted source head."""
    rows = await db.fetch_bounded(
        """WITH missing AS (
          SELECT o.*, ccr.character_instance_id AS origin_instance_id, ccr.claim_kind,
                 dn.kind::text AS node_kind
          FROM aios.character_runtime_state rs
          JOIN aios.timeline rt ON rt.timeline_id=rs.timeline_id
          JOIN aios.timeline st ON st.timeline_id=rs.source_timeline_id
          JOIN aios.dag_node head ON head.node_id=rs.source_head_node_id
            AND head.timeline_id=st.timeline_id
          JOIN aios.dag_node dn ON dn.timeline_id=st.timeline_id
            AND dn.event_id<=head.event_id
          JOIN aios.ingest_event ie ON ie.event_id=dn.event_id
          JOIN aios.observation o ON o.dag_node_id=dn.node_id AND o.timeline_id=st.timeline_id
          LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
          LEFT JOIN aios.claim_semantic_frame_projection sfp ON sfp.claim_id=o.claim_id
          LEFT JOIN aios.claim_semantic_frame f ON f.frame_id=sfp.primary_frame_id
          WHERE rs.instance_id=$1 AND o.proposition_id IS NOT NULL
            AND rt.session_id IS NOT DISTINCT FROM st.session_id
            AND rt.user_name IS NOT DISTINCT FROM st.user_name AND rt.scope_key=st.scope_key
            AND dn.kind::text IN ('chat_message','observation') AND ie.superseded_at IS NULL
            AND lower(COALESCE(f.modality,'asserted')) NOT IN
              ('hypothetical','conditional','counterfactual','question')
            AND EXISTS (
              SELECT 1 FROM aios.message_participant mp
              JOIN aios.conversation_participant cp ON cp.participant_id=mp.participant_id
              WHERE mp.node_id=dn.node_id AND mp.perceived AND cp.active
                AND cp.timeline_id=st.timeline_id AND cp.character_instance_id=rs.instance_id
            )
            AND NOT EXISTS (SELECT 1 FROM aios.knowledge_acquisition_event k
              WHERE k.instance_id=$1 AND k.claim_id=o.claim_id AND k.proposition_id=o.proposition_id)
          ORDER BY dn.event_id DESC,o.observation_id LIMIT 128
          FOR UPDATE OF o SKIP LOCKED
        )
        INSERT INTO aios.knowledge_acquisition_event
          (instance_id,proposition_id,claim_id,acquisition_mode,epistemic_status,
           confidence,dag_node_id,meta)
        SELECT $1,proposition_id,claim_id,
          aios.acquisition_mode_for_perceiver(node_kind,origin_instance_id,$1),
          CASE upper(COALESCE(claim_kind,'UNKNOWN'))
            WHEN 'BELIEF' THEN 'believed' WHEN 'MEMORY' THEN 'remembered'
            WHEN 'GOAL' THEN 'intended' WHEN 'RULE' THEN 'accepted_rule' ELSE 'observed' END,
          1.0,dag_node_id,jsonb_build_object('acquisition_semantics','perceiver-v1',
            'observation_id',observation_id,'source_key',source_key,'source_kind',source_kind,
            'recovery','runtime-source-cursor-v1','evidence_origin_preserved',true,
            'confidence_semantics','perception_delivery','semantic_confidence_separate',true)
        FROM missing RETURNING acquisition_id""", instance_id,
    )
    return len(rows)


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


async def ensure_source_character_identity(
    db: Database, *, source_actor_id: str, source_namespace: str,
    display_name: str | None = None, human_controlled: bool = False,
) -> str:
    """Create a source-qualified cognitive identity when an adapter is authoritative."""
    existing = await resolve_character_identity(db, source_actor_id)
    if existing:
        return existing
    safe_namespace = source_namespace.strip().lower().replace(" ", "-")
    character_id = f"{safe_namespace}:{source_actor_id}"
    await db.execute(
        """INSERT INTO aios.character_identity(
             character_id,canonical_name,display_name,entity_type,meta)
           VALUES($1,$2,$3,'character',$4::jsonb)
           ON CONFLICT (character_id) DO NOTHING""",
        character_id, display_name or source_actor_id, display_name or source_actor_id,
        json.dumps({
            "source_created": True,
            "source_namespace": safe_namespace,
            "controller_type": "human" if human_controlled else "agent",
        }),
    )
    return character_id


async def ensure_cognitive_instance(
    db: Database, *, character_id: str, source_namespace: str,
) -> UUID:
    """Create a lightweight persistent cognition target without activating a HUD runtime."""
    existing = await db.fetchrow(
        """SELECT instance_id FROM aios.character_instance
           WHERE character_id=$1 AND meta->>'conversation_identity_namespace'=$2
           ORDER BY created_at LIMIT 1""",
        character_id, source_namespace,
    )
    if existing:
        return existing["instance_id"]
    world_id = await ensure_character_root_world(db, character_id=character_id)
    row = await db.execute_returning_row(
        """INSERT INTO aios.character_instance(character_id,world_id,current_world_id,meta)
           VALUES($1,$2,$2,$3::jsonb) RETURNING instance_id""",
        character_id, world_id,
        json.dumps({
            "conversation_identity_namespace": source_namespace,
            "lightweight_cognitive_instance": True,
        }),
    )
    return row["instance_id"]
