# aios_app/supervisor.py

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from aios_app.config import settings
from aios_app.db import Database
from aios_app.pipeline.jobs import enqueue_job

logger = logging.getLogger("aios.supervisor")


# =================================================
# Stage definition
# =================================================

@dataclass(frozen=True)
class Stage:
    """
    Declarative description of a pipeline stage.

    The supervisor:
      - runs eligibility_sql (must EXCLUDE already queued/running jobs)
      - enqueues jobs of job_type
      - passes payload built from each row

    IMPORTANT:
      eligibility_sql MUST include a NOT EXISTS against pipeline_job
      to prevent infinite enqueue spam.
    """
    name: str
    job_type: str
    eligibility_sql: str
    payload_builder: Callable[[Dict[str, object]], Dict[str, object]]


# =================================================
# Payload builders
# =================================================

def node_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"node_id": str(row["node_id"])}

def section_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"section_id": str(row["section_id"])}

def sentence_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"sentence_id": str(row["sentence_id"])}


# =================================================
# Pipeline stages (STRUCTURAL, NOT SEMANTIC)
# =================================================

STAGES: List[Stage] = [

    # -------------------------------------------------
    # 1) DAG node → document_section
    # -------------------------------------------------
    Stage(
        name="dag_to_document_section",
        job_type="dag_to_document_section",
        eligibility_sql="""
        SELECT n.node_id
        FROM aios.dag_node n
        WHERE n.message_text IS NOT NULL
          AND (
              -- Web/doc paragraphs must have required payload fields
              (
                  n.kind = 'paragraph'
                  AND n.payload ? 'document_id'
                  AND n.payload ? 'paragraph_index'
              )
              OR
              -- Chat messages must have an event_id (used for ordering / section_path)
              (
                  n.kind = 'chat_message'
                  AND n.event_id IS NOT NULL
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aios.document_section ds
              WHERE ds.node_id = n.node_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type = 'dag_to_document_section'
                AND pj.status IN ('queued', 'running')
                AND (pj.payload->>'node_id') = n.node_id::text
          )
        ORDER BY n.created_at
        LIMIT $1
        """,
        payload_builder=node_id_payload,
    ),


    # -------------------------------------------------
    # 2) document_section → claim_candidate
    # -------------------------------------------------
    Stage(
        name="extract_claims",
        job_type="extract_claims",
        eligibility_sql="""
        SELECT es.sentence_id
        FROM aios.extracted_sentence es
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.claim_candidate cc
            WHERE cc.sentence_id = es.sentence_id
        )
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type = 'extract_claims'
                AND pj.status IN ('queued', 'running')
                AND (pj.payload->>'sentence_id') = es.sentence_id::text
          )
        ORDER BY es.sentence_id
        LIMIT $1
        """,
        payload_builder=sentence_id_payload,
    ),
]


# =================================================
# Backpressure helpers
# =================================================

async def queued_job_count(db: Database) -> int:
    row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM aios.pipeline_job WHERE status = 'queued'"
    )
    return int(row["cnt"])


# =================================================
# Supervisor internals
# =================================================

async def enqueue_stage_jobs(
    db: Database,
    stage: Stage,
    *,
    batch_size: int,
) -> int:
    rows: Iterable[Dict[str, object]] = await db.fetch(stage.eligibility_sql, batch_size)

    count = 0
    for row in rows:
        payload = stage.payload_builder(dict(row))
        await enqueue_job(db, job_type=stage.job_type, payload=payload)
        count += 1

    return count


# =================================================
# Supervisor loop
# =================================================

async def run_supervisor(
    poll_interval: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_jobs_per_cycle: Optional[int] = None,
    max_queued_backlog: Optional[int] = None,
) -> None:
    """
    Long-running orchestration loop.

    Fixes enqueue spam by:
      - excluding already queued/running jobs in each stage eligibility_sql
      - applying global backpressure (max queued backlog)
      - emitting one summary log line per cycle
    """
    poll_interval = poll_interval or settings.supervisor_poll_interval
    batch_size = batch_size or settings.supervisor_batch_size
    max_jobs_per_cycle = max_jobs_per_cycle or settings.supervisor_max_jobs_per_cycle

    if max_queued_backlog is None:
        max_queued_backlog = getattr(settings, "supervisor_max_queued_backlog", 500)

    db = Database(settings.db_dsn)
    await db.connect()

    logger.info(
        "AIOS supervisor started "
        "(poll_interval=%s batch_size=%s max_jobs_per_cycle=%s max_queued_backlog=%s)",
        poll_interval,
        batch_size,
        max_jobs_per_cycle,
        max_queued_backlog,
    )

    try:
        while True:
            qcnt = await queued_job_count(db)
            if qcnt >= max_queued_backlog:
                logger.info(
                    "Backpressure: queued=%d >= max_queued_backlog=%d (sleeping %ss)",
                    qcnt,
                    max_queued_backlog,
                    poll_interval,
                )
                await asyncio.sleep(poll_interval)
                continue

            scheduled_total = 0
            scheduled_by_stage: Dict[str, int] = {}
            remaining_budget = max_jobs_per_cycle

            for stage in STAGES:
                if remaining_budget <= 0:
                    break

                try:
                    n = await enqueue_stage_jobs(
                        db,
                        stage,
                        batch_size=min(batch_size, remaining_budget),
                    )
                    if n:
                        scheduled_by_stage[stage.name] = n
                    scheduled_total += n
                    remaining_budget -= n
                except Exception:
                    logger.exception("Stage '%s' failed during enqueue", stage.name)

            if scheduled_total:
                parts = ", ".join(f"{k}={v}" for k, v in scheduled_by_stage.items())
                logger.info("Scheduled %d jobs (%s)", scheduled_total, parts)
                continue

            await asyncio.sleep(poll_interval)

    finally:
        await db.close()


# =================================================
# CLI entrypoint
# =================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervisor())
