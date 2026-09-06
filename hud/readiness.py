from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from aios_app.db import Database
from aios_app.pipeline.jobs import enqueue_job

LIVE_PRIORITY = 15
READY_STATUS = {"ready"}


async def ensure_readiness_row(
    db: Database,
    *,
    instance_id: UUID,
    live: bool = True,
) -> None:
    state = await db.fetchrow(
        """
        SELECT rs.source_timeline_id, rs.source_head_node_id, rs.state_version,
               dn.event_id AS source_head_event_id
        FROM aios.character_runtime_state rs
        LEFT JOIN aios.dag_node dn ON dn.node_id=rs.source_head_node_id
        WHERE rs.instance_id=$1
        """,
        instance_id,
    )
    if not state:
        return
    await db.execute(
        """
        INSERT INTO aios.character_hud_readiness (
            instance_id, source_timeline_id, source_head_node_id,
            source_head_event_id, status, live, dirty_since
        )
        VALUES ($1,$2,$3,$4,'dirty',$5,now())
        ON CONFLICT (instance_id) DO UPDATE
        SET source_timeline_id=EXCLUDED.source_timeline_id,
            source_head_node_id=EXCLUDED.source_head_node_id,
            source_head_event_id=EXCLUDED.source_head_event_id,
            live=EXCLUDED.live,
            status=CASE
                WHEN aios.character_hud_readiness.prepared_source_node_id
                     IS NOT DISTINCT FROM EXCLUDED.source_head_node_id
                 AND aios.character_hud_readiness.prepared_state_version=$6
                THEN aios.character_hud_readiness.status
                ELSE 'dirty'
            END,
            dirty_since=CASE
                WHEN aios.character_hud_readiness.prepared_source_node_id
                     IS DISTINCT FROM EXCLUDED.source_head_node_id
                  OR aios.character_hud_readiness.prepared_state_version IS DISTINCT FROM $6
                THEN COALESCE(aios.character_hud_readiness.dirty_since, now())
                ELSE aios.character_hud_readiness.dirty_since
            END,
            updated_at=now()
        """,
        instance_id,
        state["source_timeline_id"],
        state["source_head_node_id"],
        state["source_head_event_id"],
        live,
        state["state_version"],
    )


async def mark_source_dirty(
    db: Database,
    *,
    instance_id: UUID,
    source_timeline_id: Optional[UUID],
    source_head_node_id: Optional[UUID],
    source_head_event_id: Optional[int],
) -> None:
    await db.execute(
        """
        INSERT INTO aios.character_hud_readiness (
            instance_id, source_timeline_id, source_head_node_id,
            source_head_event_id, status, live, dirty_since
        )
        VALUES ($1,$2,$3,$4,'dirty',true,now())
        ON CONFLICT (instance_id) DO UPDATE
        SET source_timeline_id=EXCLUDED.source_timeline_id,
            source_head_node_id=EXCLUDED.source_head_node_id,
            source_head_event_id=EXCLUDED.source_head_event_id,
            status='dirty',
            dirty_since=COALESCE(aios.character_hud_readiness.dirty_since, now()),
            updated_at=now()
        """,
        instance_id,
        source_timeline_id,
        source_head_node_id,
        source_head_event_id,
    )


async def mark_matching_runtime_dirty(
    db: Database,
    *,
    character_id: str,
    session_id: Optional[UUID],
    user_name: Optional[str],
    scope_key: str,
    source_timeline_id: UUID,
    source_head_node_id: UUID,
    source_head_event_id: int,
) -> None:
    rows = await db.fetch(
        """
        SELECT rs.instance_id
        FROM aios.character_runtime_state rs
        JOIN aios.character_instance ci ON ci.instance_id=rs.instance_id
        JOIN aios.timeline rt ON rt.timeline_id=rs.timeline_id
        WHERE ci.character_id=$1
          AND rt.session_id IS NOT DISTINCT FROM $2
          AND rt.user_name IS NOT DISTINCT FROM $3
          AND rt.scope_key=$4
          AND rs.source_timeline_id=$5
          AND rs.source_head_node_id=$6
        """,
        character_id,
        session_id,
        user_name,
        scope_key,
        source_timeline_id,
        source_head_node_id,
    )
    for row in rows:
        await mark_source_dirty(
            db,
            instance_id=row["instance_id"],
            source_timeline_id=source_timeline_id,
            source_head_node_id=source_head_node_id,
            source_head_event_id=source_head_event_id,
        )


async def source_node_retrieval_ready(
    db: Database,
    *,
    instance_id: UUID,
    node_id: Optional[UUID],
) -> bool:
    if node_id is None:
        return True

    section = await db.fetchrow(
        """
        SELECT ds.section_id, ds.claims_extracted_at
        FROM aios.document_section ds
        WHERE ds.node_id=$1
        ORDER BY ds.section_order
        LIMIT 1
        """,
        node_id,
    )
    if not section or section["claims_extracted_at"] is None:
        return False

    claim_counts = await db.fetchrow(
        """
        SELECT
            count(DISTINCT cc.claim_id) AS total,
            count(DISTINCT ccr.claim_id) AS contextualized,
            count(DISTINCT o.claim_id) AS normalized,
            count(DISTINCT CASE WHEN kae.processed_at IS NOT NULL THEN cc.claim_id END)
                AS knowledge_ready,
            count(DISTINCT CASE WHEN stp.projected_at IS NOT NULL THEN cc.claim_id END)
                AS topology_ready
        FROM aios.document_section ds
        LEFT JOIN aios.extracted_sentence es ON es.section_id=ds.section_id
        LEFT JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
        LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
        LEFT JOIN aios.observation o ON o.claim_id=cc.claim_id
        LEFT JOIN aios.knowledge_acquisition_event kae
          ON kae.claim_id=cc.claim_id AND kae.instance_id=$2
        LEFT JOIN aios.semantic_topology_projection stp
          ON stp.claim_id=cc.claim_id
         AND stp.resolver_version='semantic-topology-v1'
        WHERE ds.node_id=$1
        """,
        node_id,
        instance_id,
    )
    total = int(claim_counts["total"] or 0)
    if total == 0:
        return True
    return (
        int(claim_counts["contextualized"] or 0) == total
        and int(claim_counts["normalized"] or 0) == total
        and int(claim_counts["knowledge_ready"] or 0) == total
        and int(claim_counts["topology_ready"] or 0) == total
    )


async def _enqueue_live_job(
    db: Database,
    *,
    job_type: str,
    payload: dict,
) -> bool:
    discriminator = next(
        (
            (key, str(payload[key]))
            for key in ("node_id", "section_id", "claim_id", "acquisition_id")
            if payload.get(key) is not None
        ),
        None,
    )
    if discriminator:
        key, value = discriminator
        exists = await db.fetchrow(
            """
            SELECT 1
            FROM aios.pipeline_job
            WHERE job_type=$1
              AND status IN ('queued','running')
              AND payload->>$2=$3
            LIMIT 1
            """,
            job_type,
            key,
            value,
        )
    else:
        exists = await db.fetchrow(
            """
            SELECT 1
            FROM aios.pipeline_job
            WHERE job_type=$1
              AND status IN ('queued','running')
            LIMIT 1
            """,
            job_type,
        )
    if exists:
        await db.execute(
            """
            UPDATE aios.pipeline_job
            SET priority=LEAST(priority,$2), updated_at=now()
            WHERE job_type=$1
              AND status IN ('queued','running')
              AND (
                    $3::text IS NULL
                    OR payload->>$3=$4
                  )
            """,
            job_type,
            LIVE_PRIORITY,
            discriminator[0] if discriminator else None,
            discriminator[1] if discriminator else None,
        )
        return False

    await enqueue_job(
        db,
        job_type=job_type,
        payload=payload,
        priority=LIVE_PRIORITY,
    )
    return True


async def enqueue_live_turn_work(
    db: Database,
    *,
    instance_id: UUID,
    node_id: Optional[UUID],
) -> int:
    if node_id is None:
        return 0

    queued = 0
    section = await db.fetchrow(
        "SELECT section_id, claims_extracted_at FROM aios.document_section WHERE node_id=$1 LIMIT 1",
        node_id,
    )
    if not section:
        created = await _enqueue_live_job(
            db,
            job_type="dag_to_document_section",
            payload={"node_id": str(node_id), "live_instance_id": str(instance_id)},
        )
        return int(created)

    section_id = section["section_id"]
    if section["claims_extracted_at"] is None:
        created = await _enqueue_live_job(
            db,
            job_type="extract_claims",
            payload={"section_id": str(section_id), "live_instance_id": str(instance_id)},
        )
        return int(created)

    claims = await db.fetch(
        """
        SELECT DISTINCT cc.claim_id
        FROM aios.claim_candidate cc
        JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
        WHERE es.section_id=$1
        """,
        section_id,
    )

    for claim in claims:
        claim_id = claim["claim_id"]
        context = await db.fetchrow(
            "SELECT 1 FROM aios.claim_context_resolution WHERE claim_id=$1",
            claim_id,
        )
        if not context:
            queued += int(await _enqueue_live_job(
                db,
                job_type="resolve_claim_context",
                payload={"claim_id": str(claim_id), "live_instance_id": str(instance_id)},
            ))
            continue

        observation = await db.fetchrow(
            "SELECT 1 FROM aios.observation WHERE claim_id=$1",
            claim_id,
        )
        if not observation:
            queued += int(await _enqueue_live_job(
                db,
                job_type="normalize_proposition",
                payload={"claim_id": str(claim_id), "live_instance_id": str(instance_id)},
            ))
            continue

        acquisition = await db.fetchrow(
            """
            SELECT acquisition_id, processed_at
            FROM aios.knowledge_acquisition_event
            WHERE instance_id=$1 AND claim_id=$2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            instance_id,
            claim_id,
        )
        if acquisition and acquisition["processed_at"] is None:
            queued += int(await _enqueue_live_job(
                db,
                job_type="project_character_knowledge",
                payload={"live_instance_id": str(instance_id)},
            ))

        topology = await db.fetchrow(
            """
            SELECT 1
            FROM aios.semantic_topology_projection
            WHERE claim_id=$1
              AND resolver_version='semantic-topology-v1'
              AND projected_at IS NOT NULL
            """,
            claim_id,
        )
        if not topology:
            queued += int(await _enqueue_live_job(
                db,
                job_type="derive_claim_topology",
                payload={"claim_id": str(claim_id), "live_instance_id": str(instance_id)},
            ))

    return queued


async def set_retrieval_ready(
    db: Database,
    *,
    instance_id: UUID,
    node_id: Optional[UUID],
) -> None:
    event_id = None
    if node_id:
        row = await db.fetchrow("SELECT event_id FROM aios.dag_node WHERE node_id=$1", node_id)
        event_id = row["event_id"] if row else None
    await db.execute(
        """
        UPDATE aios.character_hud_readiness
        SET retrieval_ready_node_id=$2,
            retrieval_ready_event_id=$3,
            updated_at=now()
        WHERE instance_id=$1
        """,
        instance_id,
        node_id,
        event_id,
    )


async def save_prepared_snapshot(
    db: Database,
    *,
    instance_id: UUID,
    source_node_id: Optional[UUID],
    state_version: int,
    hud_json: dict,
    hud_text: str,
) -> None:
    event_id = None
    if source_node_id:
        row = await db.fetchrow("SELECT event_id FROM aios.dag_node WHERE node_id=$1", source_node_id)
        event_id = row["event_id"] if row else None
    await db.execute(
        """
        UPDATE aios.character_hud_readiness
        SET prepared_source_node_id=$2,
            prepared_source_event_id=$3,
            prepared_state_version=$4,
            status='ready',
            prepared_at=now(),
            dirty_since=NULL,
            last_error=NULL,
            hud_json=$5::jsonb,
            hud_text=$6,
            updated_at=now()
        WHERE instance_id=$1
        """,
        instance_id,
        source_node_id,
        event_id,
        state_version,
        json.dumps(hud_json),
        hud_text,
    )


async def readiness_state(db: Database, *, instance_id: UUID) -> dict:
    row = await db.fetchrow(
        "SELECT * FROM aios.character_hud_readiness WHERE instance_id=$1",
        instance_id,
    )
    return dict(row) if row else {}
