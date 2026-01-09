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

# -------------------------------------------------
# Payload builders
# -------------------------------------------------

def document_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"document_id": str(row["document_id"])}

def node_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"node_id": str(row["node_id"])}

def section_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"section_id": str(row["section_id"])}

def sentence_id_payload(row: Dict[str, object]) -> Dict[str, object]:
    return {"sentence_id": str(row["sentence_id"])}

# -------------------------------------------------
# Declarative DAG-hinged pipeline
# -------------------------------------------------

STAGES: List[Stage] = [

    # =================================================
    # 1. source_document → DAG (hinge already handled
    #    by accumulator / chat ingestion)
    # =================================================
    # ⚠️ Intentionally omitted here
    # Supervisor begins AFTER DAG exists

    # =================================================
    # 2. DAG paragraph nodes → document_section
    # =================================================
    Stage(
        name="dag_to_document_section",
        job_type="dag_to_document_section",
        eligibility_sql="""
        SELECT dn.node_id
        FROM aios.dag_node dn
        WHERE dn.kind = 'paragraph'
          AND NOT EXISTS (
              SELECT 1
              FROM aios.document_section ds
              WHERE ds.node_id = dn.node_id
          )
        ORDER BY dn.created_at
        LIMIT $1
        """,
        payload_builder=node_id_payload,
    ),

    # =================================================
    # 3. document_section → extracted_sentence
    #    (canonical sentence store)
    # =================================================
    Stage(
        name="extract_sentences",
        job_type="extract_sentences",
        eligibility_sql="""
        SELECT ds.section_id
        FROM aios.document_section ds
        WHERE NOT EXISTS (
            SELECT 1
            FROM aios.extracted_sentence es
            WHERE es.section_id = ds.section_id
        )
        ORDER BY ds.section_order
        LIMIT $1
        """,
        payload_builder=section_id_payload,
    ),

    # =================================================
    # 4. extracted_sentence → claim_candidate
    # =================================================
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
        ORDER BY es.sentence_id
        LIMIT $1
        """,
        payload_builder=sentence_id_payload,
    ),
]

# -------------------------------------------------
# Supervisor internals
# -------------------------------------------------

async def enqueue_stage_jobs(
    db: Database,
    stage: Stage,
    *,
    batch_size: int,
) -> int:
    rows: Iterable[Dict[str, object]] = await db.fetch(
        stage.eligibility_sql,
        batch_size,
    )

    count = 0
    for row in rows:
        payload = stage.payload_builder(dict(row))
        await enqueue_job(
            db,
            job_type=stage.job_type,
            payload=payload,
        )
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
        "AIOS supervisor started "
        "(poll_interval=%s batch_size=%s max_jobs_per_cycle=%s)",
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
                    scheduled += await enqueue_stage_jobs(
                        db,
                        stage,
                        batch_size=min(
                            batch_size,
                            max_jobs_per_cycle - scheduled,
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Stage %s failed during enqueue",
                        stage.name,
                    )

            if scheduled == 0:
                await asyncio.sleep(poll_interval)

    finally:
        await db.close()

# -------------------------------------------------
# CLI
# -------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervisor())
