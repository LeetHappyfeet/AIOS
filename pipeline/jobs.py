# aios_app/pipeline/jobs.py

from __future__ import annotations

import json
from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime

from ..db import Database


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
            status
        )
        VALUES (
            $1,
            $2::jsonb,
            $3,
            COALESCE($4, now()),
            'queued'
        )
        RETURNING job_id
        """,
        job_type,
        json.dumps(payload),
        priority,
        run_after,
    )

    return row["job_id"]


# ---------------------------------------------------------------------
# Atomic fetch + claim
# ---------------------------------------------------------------------

async def fetch_next_job(db: Database) -> Optional[Dict[str, Any]]:
    """
    Atomically claim the next runnable job.

    The previous implementation selected FOR UPDATE through one pooled
    connection and then marked the job running through another connection.
    The row lock was therefore released before the state transition, allowing
    multiple runners to claim the same job. This single UPDATE statement keeps
    selection, SKIP LOCKED, and the queued→running transition atomic.
    """

    row = await db.execute_returning_row(
        """
        WITH next_job AS (
            SELECT job_id
            FROM aios.pipeline_job
            WHERE status = 'queued'
              AND run_after <= now()
            ORDER BY priority ASC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE aios.pipeline_job pj
        SET status = 'running',
            attempts = pj.attempts + 1,
            updated_at = now()
        FROM next_job nj
        WHERE pj.job_id = nj.job_id
        RETURNING pj.job_id, pj.job_type, pj.payload
        """
    )

    if not row:
        return None

    job = dict(row)
    if isinstance(job.get("payload"), str):
        job["payload"] = json.loads(job["payload"])

    return job


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


async def mark_done(db: Database, job_id: UUID) -> None:
    await db.execute(
        """
        UPDATE aios.pipeline_job
        SET status = 'done',
            updated_at = now()
        WHERE job_id = $1
        """,
        job_id,
    )


async def mark_failed(
    db: Database,
    job_id: UUID,
    error: str,
) -> None:
    await db.execute(
        """
        UPDATE aios.pipeline_job
        SET status = 'failed',
            last_error = $2,
            updated_at = now()
        WHERE job_id = $1
        """,
        job_id,
        error[:2000],
    )


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
            updated_at = now()
        WHERE job_id = $1
        """,
        job_id,
        delay_seconds,
    )
