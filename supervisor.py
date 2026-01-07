# aios_app/supervisor.py

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from .config import settings
from .db import Database
from .pipeline.jobs import enqueue_job

logger = logging.getLogger("aios.supervisor")


# -------------------------------------------------
# Stage definition
# -------------------------------------------------

@dataclass(frozen=True)
class Stage:
    name: str
    job_type: str
    eligibility_sql: str
    payload_builder: Callable[[Dict[str, object]], Dict[str, object]]


def _document_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"document_id": str(row["document_id"])}


# -------------------------------------------------
# Declarative stages
# -------------------------------------------------

STAGES: List[Stage] = [
    # -------------------------------------------------
    # Split source documents into document sections
    # -------------------------------------------------
    Stage(
        name="split_sections",
        job_type="split_sections",
        eligibility_sql="""
        SELECT sd.document_id
        FROM aios.source_document sd
        LEFT JOIN aios.pipeline_job pj_done
          ON pj_done.job_type = 'split_sections'
         AND pj_done.status = 'completed'
         AND pj_done.payload->>'document_id' = sd.document_id::text
        WHERE pj_done.job_id IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj_active
            WHERE pj_active.job_type = $1
              AND pj_active.status IN ('queued', 'running')
              AND pj_active.payload->>'document_id' = sd.document_id::text
          )
        ORDER BY sd.retrieved_at ASC
        LIMIT $2
        """,
        payload_builder=_document_id_payload,
    ),

    # -------------------------------------------------
    # Split document sections into sentences
    # -------------------------------------------------
    Stage(
        name="split_sentences",
        job_type="split_sentences",
        eligibility_sql="""
        SELECT DISTINCT sd.document_id
        FROM aios.document_section ds
        JOIN aios.dag_node dn
          ON dn.node_id = ds.node_id
        JOIN aios.source_document sd
          ON sd.document_id = (dn.payload->>'document_id')::uuid
        LEFT JOIN aios.pipeline_job pj_done
          ON pj_done.job_type = 'split_sentences'
         AND pj_done.status = 'completed'
         AND pj_done.payload->>'document_id' = sd.document_id::text
        WHERE pj_done.job_id IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj_active
            WHERE pj_active.job_type = $1
              AND pj_active.status IN ('queued', 'running')
              AND pj_active.payload->>'document_id' = sd.document_id::text
          )
        ORDER BY sd.document_id
        LIMIT $2
        """,
        payload_builder=_document_id_payload,
    ),

    # -------------------------------------------------
    # Extract claims from sentences
    # -------------------------------------------------
    Stage(
        name="extract_claims",
        job_type="extract_claims",
        eligibility_sql="""
        SELECT DISTINCT sd.document_id
        FROM aios.extracted_sentence es
        JOIN aios.document_section ds
          ON ds.section_id = es.section_id
        JOIN aios.dag_node dn
          ON dn.node_id = ds.node_id
        JOIN aios.source_document sd
          ON sd.document_id = (dn.payload->>'document_id')::uuid
        LEFT JOIN aios.claim_candidate cc
          ON cc.sentence_id = es.sentence_id
        LEFT JOIN aios.pipeline_job pj_done
          ON pj_done.job_type = 'extract_claims'
         AND pj_done.status = 'completed'
         AND pj_done.payload->>'document_id' = sd.document_id::text
        WHERE cc.claim_id IS NULL
          AND pj_done.job_id IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj_active
            WHERE pj_active.job_type = $1
              AND pj_active.status IN ('queued', 'running')
              AND pj_active.payload->>'document_id' = sd.document_id::text
          )
        ORDER BY sd.document_id
        LIMIT $2
        """,
        payload_builder=_document_id_payload,
    ),
]


# -------------------------------------------------
# Supervisor internals
# -------------------------------------------------

async def _enqueue_stage_jobs(
    db: Database,
    stage: Stage,
    *,
    batch_size: int,
) -> int:
    rows: Iterable[Dict[str, object]] = await db.fetch(
        stage.eligibility_sql,
        stage.job_type,
        batch_size,
    )

    count = 0

    for row in rows:
        payload = stage.payload_builder(dict(row))
        await enqueue_job(db, job_type=stage.job_type, payload=payload)
        count += 1

    if count:
        logger.info("Enqueued %d %s jobs", count, stage.name)

    return count


# -------------------------------------------------
# Supervisor entrypoint
# -------------------------------------------------

async def run_supervisor(
    poll_interval: Optional[float] = None,
    batch_size: Optional[int] = None,
    max_jobs_per_cycle: Optional[int] = None,
) -> None:
    poll_interval = poll_interval or settings.supervisor_poll_interval
    batch_size = batch_size or settings.supervisor_batch_size
    max_jobs_per_cycle = max_jobs_per_cycle or settings.supervisor_max_jobs_per_cycle

    db = Database(settings.db_dsn)
    await db.connect()

    logger.info(
        "AIOS supervisor started (poll_interval=%s batch_size=%s max_jobs_per_cycle=%s)",
        poll_interval,
        batch_size,
        max_jobs_per_cycle,
    )

    try:
        while True:
            scheduled = 0

            for stage in STAGES:
                if scheduled >= max_jobs_per_cycle:
                    break

                try:
                    scheduled += await _enqueue_stage_jobs(
                        db,
                        stage,
                        batch_size=min(batch_size, max_jobs_per_cycle - scheduled),
                    )
                except Exception as exc:
                    logger.exception(
                        "Stage %s failed to enqueue jobs: %s",
                        stage.name,
                        exc,
                    )

            if scheduled == 0:
                await asyncio.sleep(poll_interval)

    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervisor())
