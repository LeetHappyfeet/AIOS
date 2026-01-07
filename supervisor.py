# aios_app/supervisor.py

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List

from .config import settings
from .db import Database
from .pipeline.jobs import enqueue_job

logger = logging.getLogger("aios.supervisor")


@dataclass(frozen=True)
class Stage:
    name: str
    job_type: str
    eligibility_sql: str
    payload_builder: Callable[[Dict[str, object]], Dict[str, object]]


def _document_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"document_id": row["document_id"]}


STAGES: List[Stage] = [
    Stage(
        name="split_sections",
        job_type="split_sections",
        eligibility_sql="""
        SELECT sd.document_id
        FROM aios.source_document sd
        LEFT JOIN aios.document_section ds
          ON ds.document_id = sd.document_id
        WHERE ds.document_id IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj
            WHERE pj.job_type = $1
              AND pj.status IN ('queued', 'running')
              AND pj.payload->>'document_id' = sd.document_id::text
          )
        ORDER BY sd.retrieved_at ASC
        LIMIT $2
        """,
        payload_builder=_document_id_payload,
    ),
    Stage(
        name="split_sentences",
        job_type="split_sentences",
        eligibility_sql="""
        SELECT ds.document_id
        FROM aios.document_section ds
        LEFT JOIN aios.extracted_sentence es
          ON es.section_id = ds.section_id
        WHERE es.section_id IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj
            WHERE pj.job_type = $1
              AND pj.status IN ('queued', 'running')
              AND pj.payload->>'document_id' = ds.document_id::text
          )
        GROUP BY ds.document_id
        ORDER BY MIN(ds.section_order) ASC
        LIMIT $2
        """,
        payload_builder=_document_id_payload,
    ),
    Stage(
        name="extract_claims",
        job_type="extract_claims",
        eligibility_sql="""
        SELECT ds.document_id
        FROM aios.document_section ds
        JOIN aios.extracted_sentence es
          ON es.section_id = ds.section_id
        LEFT JOIN aios.claim_candidate cc
          ON cc.sentence_id = es.sentence_id
        WHERE cc.claim_id IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj
            WHERE pj.job_type = $1
              AND pj.status IN ('queued', 'running')
              AND pj.payload->>'document_id' = ds.document_id::text
          )
        GROUP BY ds.document_id
        ORDER BY MIN(es.sentence_id) ASC
        LIMIT $2
        """,
        payload_builder=_document_id_payload,
    ),
]


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


async def run_supervisor(
    poll_interval: float | None = None,
    batch_size: int | None = None,
) -> None:
    poll_interval = poll_interval if poll_interval is not None else settings.supervisor_poll_interval
    batch_size = batch_size if batch_size is not None else settings.supervisor_batch_size

    db = Database(settings.db_dsn)
    await db.connect()

    logger.info(
        "AIOS supervisor started (poll_interval=%s batch_size=%s)",
        poll_interval,
        batch_size,
    )

    try:
        while True:
            scheduled = 0
            for stage in STAGES:
                try:
                    scheduled += await _enqueue_stage_jobs(
                        db,
                        stage,
                        batch_size=batch_size,
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
