# aios_app/runner.py

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable, Dict, Any
from uuid import UUID

from aios_app.config import settings
from aios_app.db import Database
from aios_app.pipeline.jobs import (
    fetch_next_job,
    mark_running,
    mark_done,
    mark_failed,
)

from aios_app.pipeline.dag_to_document_section_worker import run_worker as run_dag_to_document_section
from aios_app.pipeline.worker import run_claim_extraction_for_section

from aios_app.rdf.fuseki import FusekiClient
from aios_app.rdf.world_liminal import promote_liminal_claims
from aios_app.rdf.world_liminal_classifier_runner import classify_liminal_claims

logger = logging.getLogger("aios.pipeline.runner")

JOB_HANDLERS: Dict[str, Callable[[Database, Dict[str, Any]], Awaitable[None]]] = {}


# -------------------------------------------------
# Existing handlers
# -------------------------------------------------

async def handle_dag_to_document_section(db: Database, job: Dict[str, Any]) -> None:
    node_id = UUID(job["payload"]["node_id"])
    await run_dag_to_document_section(db, node_id=node_id)


async def handle_extract_claims(db: Database, job: Dict[str, Any]) -> None:
    section_id = UUID(job["payload"]["section_id"])
    await run_claim_extraction_for_section(db, section_id=section_id)


# -------------------------------------------------
# NEW: RDF handlers
# -------------------------------------------------

async def handle_rdf_liminal_promote(db: Database, job: Dict[str, Any]) -> None:
    fuseki = FusekiClient(settings.fuseki_base_url)
    await promote_liminal_claims(db, fuseki, batch_size=500)


async def handle_rdf_liminal_classify(db: Database, job: Dict[str, Any]) -> None:
    fuseki = FusekiClient(settings.fuseki_base_url)
    await classify_liminal_claims(db, fuseki, batch_size=500)


JOB_HANDLERS.update(
    {
        "dag_to_document_section": handle_dag_to_document_section,
        "extract_claims": handle_extract_claims,
        "rdf_liminal_promote": handle_rdf_liminal_promote,
        "rdf_liminal_classify": handle_rdf_liminal_classify,
    }
)


# -------------------------------------------------
# Runner loop
# -------------------------------------------------

async def run_runner(poll_interval: float = 1.0) -> None:
    db = Database(settings.db_dsn)
    await db.connect()

    logger.info("Pipeline runner started")

    try:
        while True:
            job = await fetch_next_job(db)
            if not job:
                await asyncio.sleep(poll_interval)
                continue

            job_id = job["job_id"]
            job_type = job["job_type"]
            handler = JOB_HANDLERS.get(job_type)

            if not handler:
                await mark_failed(db, job_id, f"No handler for job_type={job_type}")
                continue

            try:
                await mark_running(db, job_id)
                await handler(db, job)
                await mark_done(db, job_id)
            except Exception as exc:
                logger.exception("Job %s (%s) failed", job_id, job_type)
                await mark_failed(db, job_id, repr(exc))

    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_runner(poll_interval=settings.runner_poll_interval))
