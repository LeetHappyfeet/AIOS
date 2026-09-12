from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.message_cognition import commit_message_cognition, mark_enrichment_ready
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
    """Adopt the exact source coordinate, dirty matching runtimes, then commit cognition."""
    from aios_app.world.source_cursor import advance_matching_runtime_source_cursor

    instance_ids = await advance_matching_runtime_source_cursor(
        db,
        character_id=character_id,
        session_id=session_id,
        user_name=user_name,
        scope_key=scope_key,
        source_timeline_id=source_timeline_id,
        source_head_node_id=source_head_node_id,
        source_head_event_id=source_head_event_id,
    )
    for instance_id in instance_ids:
        await mark_source_dirty(
            db,
            instance_id=instance_id,
            source_timeline_id=source_timeline_id,
            source_head_node_id=source_head_node_id,
            source_head_event_id=source_head_event_id,
        )
        await enqueue_live_turn_work(
            db,
            instance_id=instance_id,
            node_id=source_head_node_id,
        )


async def source_node_retrieval_ready(
    db: Database,
    *,
    instance_id: UUID,
    node_id: Optional[UUID],
) -> bool:
    """Return the generation-critical cognitive barrier for one source node.

    The old implementation waited for every extracted claim, normalization,
    acquisition and topology projection. That made semantic archaeology a hard
    dependency of generation. Retrieval readiness now means that the bounded
    message-level cognitive commit exists for this exact runtime/source node.
    Exhaustive enrichment continues independently.
    """
    if node_id is None:
        return True
    row = await db.fetchrow(
        """
        SELECT 1
        FROM aios.message_cognitive_commit
        WHERE instance_id=$1
          AND node_id=$2
          AND interpreter_version='message-cognition-v1'
        """,
        instance_id,
        node_id,
    )
    return bool(row)


async def source_node_topology_ready(
    db: Database,
    *,
    instance_id: UUID,
    node_id: Optional[UUID],
) -> bool:
    """Return whether exhaustive topology enrichment is complete for diagnostics.

    This is no longer a generation barrier. When complete, the separate
    enrichment cursor is advanced so operators can see cognition versus
    archaeology progress independently.
    """
    if node_id is None:
        return True
    row = await db.fetchrow(
        """
        WITH runtime_identity AS (
            SELECT ci.character_id
            FROM aios.character_instance ci
            WHERE ci.instance_id=$2
        )
        SELECT
            count(DISTINCT cc.claim_id) AS total,
            count(DISTINCT CASE WHEN stp.projected_at IS NOT NULL THEN cc.claim_id END)
                AS claim_topology_ready,
            count(DISTINCT CASE
                WHEN ccr.epistemic_scope='character'
                 AND ccr.origin_character_id=ri.character_id
                THEN cc.claim_id
            END) AS acquisition_required,
            count(DISTINCT CASE
                WHEN ccr.epistemic_scope='character'
                 AND ccr.origin_character_id=ri.character_id
                 AND ccr.character_instance_id=$2
                 AND kae.processed_at IS NOT NULL
                 AND astp.projected_at IS NOT NULL
                THEN cc.claim_id
            END) AS acquisition_topology_ready
        FROM aios.document_section ds
        CROSS JOIN runtime_identity ri
        LEFT JOIN aios.extracted_sentence es ON es.section_id=ds.section_id
        LEFT JOIN aios.claim_candidate cc ON cc.sentence_id=es.sentence_id
        LEFT JOIN aios.claim_context_resolution ccr ON ccr.claim_id=cc.claim_id
        LEFT JOIN aios.knowledge_acquisition_event kae
          ON kae.claim_id=cc.claim_id AND kae.instance_id=$2
        LEFT JOIN aios.semantic_topology_projection stp
          ON stp.claim_id=cc.claim_id
         AND stp.resolver_version='semantic-topology-v1'
        LEFT JOIN aios.semantic_topology_projection astp
          ON astp.acquisition_id=kae.acquisition_id
         AND astp.resolver_version='semantic-topology-v1'
        WHERE ds.node_id=$1
        """,
        node_id,
        instance_id,
    )
    if not row:
        return False
    total = int(row["total"] or 0)
    ready = total == 0 or (
        int(row["claim_topology_ready"] or 0) == total
        and int(row["acquisition_topology_ready"] or 0)
        == int(row["acquisition_required"] or 0)
    )
    if ready:
        await mark_enrichment_ready(db, instance_id=instance_id, node_id=node_id)
    return ready


async def _enqueue_live_job(
    db: Database,
    *,
    job_type: str,
    payload: dict,
) -> bool:
    """Compatibility helper for the few live jobs that remain pipeline-backed."""
    discriminator = next(
        (
            (key, str(payload[key]))
            for key in ("node_id", "section_id", "claim_id", "acquisition_id", "live_instance_id")
            if payload.get(key) is not None
        ),
        None,
    )
    if discriminator:
        key, value = discriminator
        exists = await db.fetchrow(
            """
            SELECT 1 FROM aios.pipeline_job
            WHERE job_type=$1 AND status IN ('queued','running') AND payload->>$2=$3
            LIMIT 1
            """,
            job_type,
            key,
            value,
        )
    else:
        exists = await db.fetchrow(
            "SELECT 1 FROM aios.pipeline_job WHERE job_type=$1 AND status IN ('queued','running') LIMIT 1",
            job_type,
        )
    if exists:
        await db.execute(
            """
            UPDATE aios.pipeline_job
            SET priority=LEAST(priority,$2), updated_at=now()
            WHERE job_type=$1
              AND status IN ('queued','running')
              AND ($3::text IS NULL OR payload->>$3=$4)
            """,
            job_type,
            LIVE_PRIORITY,
            discriminator[0] if discriminator else None,
            discriminator[1] if discriminator else None,
        )
        return False
    await enqueue_job(db, job_type=job_type, payload=payload, priority=LIVE_PRIORITY)
    return True


async def enqueue_live_turn_work(
    db: Database,
    *,
    instance_id: UUID,
    node_id: Optional[UUID],
) -> int:
    """Commit generation-critical cognition without promoting exhaustive claims.

    Message cognition is deliberately bounded and SQL-only, so it can complete
    synchronously at source-cursor advance. Claim extraction, frame decomposition,
    detailed context resolution, RDF, topology, vectors and reconciliation remain
    under the ordinary supervisor as asynchronous enrichment.
    """
    if node_id is None:
        return 0
    committed = await commit_message_cognition(
        db,
        instance_id=instance_id,
        node_id=node_id,
    )
    if committed:
        return 0

    # A missing DAG coordinate is exceptional, but preserve the existing repair
    # path rather than fanning out per-claim LIVE work.
    created = await _enqueue_live_job(
        db,
        job_type="dag_to_document_section",
        payload={"node_id": str(node_id), "live_instance_id": str(instance_id)},
    )
    return int(created)


async def set_retrieval_ready(
    db: Database,
    *,
    instance_id: UUID,
    node_id: Optional[UUID],
) -> None:
    """Compatibility setter: retrieval readiness now aliases cognitive readiness."""
    event_id = None
    if node_id:
        row = await db.fetchrow("SELECT event_id FROM aios.dag_node WHERE node_id=$1", node_id)
        event_id = row["event_id"] if row else None
    await db.execute(
        """
        UPDATE aios.character_hud_readiness
        SET cognitive_ready_node_id=$2,
            cognitive_ready_event_id=$3,
            retrieval_ready_node_id=$2,
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

    serialized_hud = json.dumps(hud_json, default=str)
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
        serialized_hud,
        hud_text,
    )


async def readiness_state(db: Database, *, instance_id: UUID) -> dict:
    row = await db.fetchrow(
        "SELECT * FROM aios.character_hud_readiness WHERE instance_id=$1",
        instance_id,
    )
    return dict(row) if row else {}
