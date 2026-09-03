# aios_app/supervisor.py

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List

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

    eligibility_sql MUST exclude already queued/running jobs.
    Payload is passed verbatim to the runner.
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


def empty_payload(_: Dict[str, object]) -> Dict[str, object]:
    return {}


# =================================================
# Pipeline stages
# =================================================

STAGES: List[Stage] = [

    # -------------------------------------------------
    # 1) DAG → document_section
    # -------------------------------------------------
    Stage(
        name="dag_to_document_section",
        job_type="dag_to_document_section",
        eligibility_sql="""
        SELECT n.node_id
        FROM aios.dag_node n
        WHERE n.message_text IS NOT NULL
          AND (
              (
                  n.kind = 'paragraph'
                  AND n.payload ? 'document_id'
                  AND n.payload ? 'paragraph_index'
              )
              OR
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
        ORDER BY n.event_id
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
        SELECT ds.section_id
        FROM aios.document_section ds
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        WHERE ds.claims_extracted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type = 'extract_claims'
                AND pj.status IN ('queued', 'running')
                AND (pj.payload->>'section_id') = ds.section_id::text
          )
        ORDER BY n.event_id
        LIMIT $1
        """,
        payload_builder=section_id_payload,
    ),

    # -------------------------------------------------
    # 3) section claim set → RDF /world/liminal
    # -------------------------------------------------
    # One job owns one section. This preserves the lineage boundary:
    # ingest_event → dag_node → document_section → claims → RDF receipts.
    Stage(
        name="rdf_liminal_promote",
        job_type="rdf_liminal_promote",
        eligibility_sql="""
        SELECT ds.section_id
        FROM aios.document_section ds
        JOIN aios.dag_node n
          ON n.node_id = ds.node_id
        JOIN aios.ingest_event ie
          ON ie.event_id = n.event_id
        WHERE ds.claims_extracted_at IS NOT NULL
          AND ie.rdf_processed_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM aios.pipeline_job pj
              WHERE pj.job_type = 'rdf_liminal_promote'
                AND pj.status IN ('queued', 'running')
                AND (pj.payload->>'section_id') = ds.section_id::text
          )
        ORDER BY n.event_id
        LIMIT $1
        """,
        payload_builder=section_id_payload,
    ),

    # -------------------------------------------------
    # 4) classify promoted liminal RDF claims
    # -------------------------------------------------
    # The old gate stopped scheduling forever after ANY contentKind receipt
    # existed. Keep scheduling while at least one base-promoted claim lacks its
    # own classification receipt.
    Stage(
        name="rdf_liminal_classify",
        job_type="rdf_liminal_classify",
        eligibility_sql="""
        SELECT 1
        WHERE EXISTS (
            SELECT 1
            FROM aios.claim_candidate cc
            WHERE EXISTS (
                SELECT 1
                FROM aios.rdf_promotion_log base
                WHERE base.claim_id = cc.claim_id
                  AND base.rdf_dataset = 'world'
                  AND base.rdf_graph = 'urn:aios:world:liminal'
                  AND base.rdf_predicate = 'rdf:type'
                  AND base.rdf_object = 'world:Claim'
            )
              AND NOT EXISTS (
                  SELECT 1
                  FROM aios.rdf_promotion_log cls
                  WHERE cls.claim_id = cc.claim_id
                    AND cls.rdf_dataset = 'world'
                    AND cls.rdf_graph = 'urn:aios:world:liminal'
                    AND cls.rdf_predicate = 'world:contentKind'
              )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM aios.pipeline_job pj
            WHERE pj.job_type = 'rdf_liminal_classify'
              AND pj.status IN ('queued', 'running')
        )
        LIMIT $1
        """,
        payload_builder=empty_payload,
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

async def run_supervisor() -> None:
    poll_interval = settings.supervisor_poll_interval
    batch_size = settings.supervisor_batch_size
    max_jobs_per_cycle = settings.supervisor_max_jobs_per_cycle
    max_queued_backlog = getattr(settings, "supervisor_max_queued_backlog", 500)

    db = Database(settings.db_dsn)
    await db.connect()

    logger.info("AIOS supervisor started")

    try:
        while True:
            qcnt = await queued_job_count(db)
            if qcnt >= max_queued_backlog:
                await asyncio.sleep(poll_interval)
                continue

            remaining = max_jobs_per_cycle
            scheduled = 0

            for stage in STAGES:
                if remaining <= 0:
                    break

                try:
                    n = await enqueue_stage_jobs(
                        db,
                        stage,
                        batch_size=min(batch_size, remaining),
                    )
                    scheduled += n
                    remaining -= n
                except Exception:
                    logger.exception("Stage '%s' enqueue failed", stage.name)

            if scheduled == 0:
                await asyncio.sleep(poll_interval)

    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_supervisor())
