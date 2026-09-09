# aios_app/pipeline/jobs.py

from __future__ import annotations

import json
from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime

from ..db import Database
from .job_registry import job_spec


# ---------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------

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
    row = await db.execute_returning_row(
        """
        INSERT INTO aios.pipeline_job (
            job_type,
            payload,
            priority,
            run_after,
            status,
            resource_class
        )
        VALUES (
            $1,
            $2::jsonb,
            $3,
            COALESCE($4, now()),
            'queued',
            $5
        )
        RETURNING job_id
        """,
        job_type,
        json.dumps(payload),
        priority,
        run_after,
        job_spec(job_type).resource_class.value,
    )

    return row["job_id"]


# ---------------------------------------------------------------------
# Atomic fetch + claim
# ---------------------------------------------------------------------

async def fetch_next_job(
    db: Database,
    *,
    worker_id: str,
    resource_class: str,
    lease_seconds: int,
) -> Optional[Dict[str, Any]]:
    """Atomically lease the next runnable job for one execution class."""

    row = await db.execute_returning_row(
        """
        WITH next_job AS (
            SELECT job_id
            FROM aios.pipeline_job
            WHERE status = 'queued'
              AND run_after <= now()
              AND resource_class = $1
            ORDER BY priority ASC, created_at ASC
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
                  pj.worker_id, pj.claimed_at, pj.lease_expires_at
        """,
        resource_class,
        worker_id,
        lease_seconds,
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
                            THEN 18
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
