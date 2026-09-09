# aios_app/pipeline/jobs.py

from __future__ import annotations

import json
from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime

from ..db import Database
from .job_registry import SchedulingLane, job_spec, scheduling_lane


# ---------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------


async def _partition_key_for_enqueue(
    db: Database,
    *,
    job_type: str,
    payload: Dict[str, Any],
) -> Optional[str]:
    if payload.get("claim_id"):
        claim_id = str(payload["claim_id"])
        if job_type in {"resolve_claim_context", "normalize_proposition"}:
            return f"claim:{claim_id}"
        if job_type in {"derive_claim_topology", "rdf_epistemic_project"}:
            row = await db.fetchrow(
                """
                SELECT CASE
                    WHEN epistemic_scope='character'
                         AND origin_character_id IS NOT NULL
                         AND character_instance_id IS NOT NULL
                        THEN 'char:' || origin_character_id
                    WHEN source_id IS NOT NULL
                        THEN 'source:' || source_id
                    WHEN world_id IS NOT NULL
                        THEN 'world:' || world_id::text || ':observed'
                    ELSE 'claim:' || claim_id::text
                END AS scope_key
                FROM aios.claim_context_resolution
                WHERE claim_id=$1::uuid
                """,
                claim_id,
            )
            if row:
                return str(row["scope_key"])
            return f"claim:{claim_id}"

    if payload.get("acquisition_id"):
        acquisition_id = str(payload["acquisition_id"])
        row = await db.fetchrow(
            """
            SELECT 'char:' || ci.character_id AS scope_key
            FROM aios.knowledge_acquisition_event kae
            JOIN aios.character_instance ci ON ci.instance_id=kae.instance_id
            WHERE kae.acquisition_id=$1::uuid
            """,
            acquisition_id,
        )
        return str(row["scope_key"]) if row else f"acquisition:{acquisition_id}"

    if payload.get("assertion_id"):
        assertion_id = str(payload["assertion_id"])
        row = await db.fetchrow(
            """
            SELECT 'world:' || world_id::text || ':asserted' AS scope_key
            FROM aios.world_proposition_assertion
            WHERE assertion_id=$1::uuid
            """,
            assertion_id,
        )
        return str(row["scope_key"]) if row else f"assertion:{assertion_id}"

    for key in ("world_id", "character_id", "section_id", "node_id"):
        if payload.get(key):
            return f"{key}:{payload[key]}"

    return None



async def enqueue_job(
    db: Database,
    *,
    job_type: str,
    payload: Dict[str, Any],
    priority: int = 100,
    run_after: Optional[datetime] = None,
) -> UUID:
    """
    Enqueue a new pipeline job.

    This is the ONLY blessed way to create jobs.
    Status will ALWAYS start as 'queued'.
    """
    lane = scheduling_lane(job_type, payload).value
    partition_key = await _partition_key_for_enqueue(
        db,
        job_type=job_type,
        payload=payload,
    )

    row = await db.execute_returning_row(
        """
        INSERT INTO aios.pipeline_job (
            job_type,
            payload,
            priority,
            run_after,
            status,
            resource_class,
            scheduling_lane,
            partition_key
        )
        VALUES (
            $1,
            $2::jsonb,
            $3,
            COALESCE($4, now()),
            'queued',
            $5,
            $6,
            $7
        )
        RETURNING job_id
        """,
        job_type,
        json.dumps(payload),
        priority,
        run_after,
        job_spec(job_type).resource_class.value,
        lane,
        partition_key,
    )

    return row["job_id"]


# ---------------------------------------------------------------------
# Atomic fetch + claim
# ---------------------------------------------------------------------

async def fetch_next_job(
    db: Database,
    *,
    worker_id: str = "legacy-dispatcher",
    resource_class: Optional[str] = None,
    lease_seconds: int = 120,
    scheduling_lanes: Optional[list[str]] = None,
    prefer_uncontended: bool = True,
) -> Optional[Dict[str, Any]]:
    """Atomically lease the next runnable job for one execution class.

    Lane filtering protects live epistemic work from maintenance starvation.
    Jobs whose partition is already running are ranked after uncontended jobs
    so workers spread across independent semantic trees before convoying.
    """

    row = await db.execute_returning_row(
        """
        WITH next_job AS (
            SELECT q.job_id
            FROM aios.pipeline_job q
            WHERE q.status = 'queued'
              AND q.run_after <= now()
              AND ($1::text IS NULL OR q.resource_class = $1)
              AND (
                    $4::text[] IS NULL
                    OR q.scheduling_lane = ANY($4::text[])
              )
            ORDER BY
                CASE
                    WHEN $5::boolean
                     AND q.partition_key IS NOT NULL
                     AND EXISTS (
                        SELECT 1
                        FROM aios.pipeline_job active
                        WHERE active.status='running'
                          AND active.partition_key=q.partition_key
                     )
                    THEN 1 ELSE 0
                END ASC,
                q.priority ASC,
                q.created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE aios.pipeline_job pj
        SET status = 'running',
            attempts = pj.attempts + 1,
            worker_id = $2,
            claimed_at = now(),
            heartbeat_at = now(),
            lease_expires_at = now() + make_interval(secs => $3),
            updated_at = now()
        FROM next_job nj
        WHERE pj.job_id = nj.job_id
        RETURNING pj.job_id, pj.job_type, pj.payload, pj.resource_class,
                  pj.scheduling_lane, pj.partition_key, pj.worker_id,
                  pj.claimed_at, pj.lease_expires_at
        """,
        resource_class,
        worker_id,
        lease_seconds,
        scheduling_lanes,
        prefer_uncontended,
    )

    if not row:
        return None

    job = dict(row)
    if isinstance(job.get("payload"), str):
        job["payload"] = json.loads(job["payload"])

    return job


async def heartbeat_job(
    db: Database,
    *,
    job_id: UUID,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    row = await db.execute_returning_row(
        """
        UPDATE aios.pipeline_job
        SET heartbeat_at=now(),
            lease_expires_at=now() + make_interval(secs => $3),
            updated_at=now()
        WHERE job_id=$1
          AND status='running'
          AND worker_id=$2
        RETURNING job_id
        """,
        job_id,
        worker_id,
        lease_seconds,
    )
    return bool(row)


# ---------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------

async def mark_running(db: Database, job_id: UUID) -> None:
    """
    Backward-compatible helper. New runners should not call this because
    fetch_next_job() now performs the queued→running transition atomically.
    """
    await db.execute(
        """
        UPDATE aios.pipeline_job
        SET status = 'running',
            attempts = attempts + 1,
            updated_at = now()
        WHERE job_id = $1
          AND status <> 'running'
        """,
        job_id,
    )


async def mark_done(db: Database, job_id: UUID, *, worker_id: Optional[str] = None) -> None:
    await db.execute(
        """
        UPDATE aios.pipeline_job
        SET status = 'done',
            worker_id=NULL,
            heartbeat_at=NULL,
            lease_expires_at=NULL,
            updated_at = now()
        WHERE job_id = $1
          AND ($2::text IS NULL OR worker_id=$2)
        """,
        job_id,
        worker_id,
    )


async def mark_failed(
    db: Database,
    job_id: UUID,
    error: str,
    *,
    worker_id: Optional[str] = None,
) -> None:
    await db.execute(
        """
        UPDATE aios.pipeline_job
        SET status = 'failed',
            last_error = $2,
            worker_id=NULL,
            heartbeat_at=NULL,
            lease_expires_at=NULL,
            updated_at = now()
        WHERE job_id = $1
          AND ($3::text IS NULL OR worker_id=$3)
        """,
        job_id,
        error[:2000],
        worker_id,
    )




async def rebalance_queued_priorities(db: Database) -> int:
    """
    Apply the current pipeline priority policy to jobs already in the queue.

    Eligibility is authoritative, so changing priority never changes what work
    a job represents; it only changes runner admission order.
    """
    row = await db.execute_returning_row(
        """
        WITH desired AS (
            SELECT
                job_id,
                CASE job_type
                    WHEN 'discover_characters' THEN 10
                    WHEN 'project_world_topology' THEN 20
                    WHEN 'dag_to_document_section' THEN 20
                    WHEN 'extract_claims' THEN 25
                    WHEN 'rdf_liminal_promote' THEN 25
                    WHEN 'rdf_liminal_classify' THEN 28
                    WHEN 'resolve_claim_context' THEN 30
                    WHEN 'project_character_knowledge' THEN 30
                    WHEN 'normalize_proposition' THEN 35
                    WHEN 'derive_character_acquisition_topology' THEN 40
                    WHEN 'derive_world_assertion_topology' THEN 45
                    WHEN 'resolve_generated_facts' THEN 60
                    WHEN 'rdf_epistemic_project' THEN 75
                    WHEN 'derive_claim_topology' THEN
                        CASE
                            WHEN payload->>'semantic_backfill'='proposition_leaves_20260909'
                            THEN 95
                            ELSE 80
                        END
                    WHEN 'assign_narratives' THEN 90
                    ELSE priority
                END AS desired_priority
            FROM aios.pipeline_job
            WHERE status='queued'
        ),
        changed AS (
            UPDATE aios.pipeline_job pj
            SET priority=d.desired_priority,
                updated_at=now()
            FROM desired d
            WHERE pj.job_id=d.job_id
              AND pj.priority IS DISTINCT FROM d.desired_priority
            RETURNING pj.job_id
        )
        SELECT COUNT(*)::integer AS cnt FROM changed
        """
    )
    return int(row["cnt"]) if row else 0


async def recover_stale_running_jobs(
    db: Database,
    *,
    stale_after_seconds: int,
) -> int:
    """Requeue jobs whose explicit worker lease has expired.

    The legacy updated_at fallback is retained only for pre-migration running
    rows that do not yet have lease metadata.
    """
    row = await db.execute_returning_row(
        """
        WITH recovered AS (
            UPDATE aios.pipeline_job
            SET status='queued',
                run_after=now(),
                worker_id=NULL,
                claimed_at=NULL,
                heartbeat_at=NULL,
                lease_expires_at=NULL,
                updated_at=now(),
                last_error=CASE
                    WHEN COALESCE(last_error,'') = '' THEN
                        '[recovered expired worker lease]'
                    ELSE
                        last_error || ' [recovered expired worker lease]'
                END
            WHERE status='running'
              AND (
                    lease_expires_at < now()
                    OR (
                        lease_expires_at IS NULL
                        AND updated_at < now() - make_interval(secs => $1)
                    )
              )
            RETURNING job_id
        )
        SELECT COUNT(*)::integer AS cnt FROM recovered
        """,
        stale_after_seconds,
    )
    return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------
# Optional retry helper
# ---------------------------------------------------------------------

async def retry_failed_job(
    db: Database,
    *,
    job_id: UUID,
    delay_seconds: int = 30,
) -> None:
    await db.execute(
        """
        UPDATE aios.pipeline_job
        SET status = 'queued',
            run_after = now() + make_interval(secs => $2),
            worker_id=NULL,
            claimed_at=NULL,
            heartbeat_at=NULL,
            lease_expires_at=NULL,
            updated_at = now()
        WHERE job_id = $1
        """,
        job_id,
        delay_seconds,
    )
