# aios_app/pipeline/runner.py

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable, Dict, Any

from aios_app.config import settings
from aios_app.db import Database
from aios_app.pipeline.jobs import (
    fetch_next_job,
    mark_running,
    mark_done,
    mark_failed,
)

# -------------------------------------------------
# Import worker entrypoints
# -------------------------------------------------

from aios_app.pipeline.worker import run_claim_extraction_worker
from aios_app.extract.sentences import run_sentence_extraction_worker
from aios_app.pipeline.dag_to_document_section_worker import (
    run_dag_to_document_section_worker,
)

logger = logging.getLogger("aios.pipeline.runner")

# -------------------------------------------------
# Job handler registry
# -------------------------------------------------

JOB_HANDLERS: Dict[str, Callable[[Database, Dict[str, Any]], Awaitable[None]]] = {}


# -------------------------------------------------
# Handlers
# -------------------------------------------------

async def _handle_dag_to_document_section(
    db: Database,
    job: Dict[str, Any],
) -> None:
    await run_dag_to_document_section_worker(db)


async def _handle_extract_sentences(
    db: Database,
    job: Dict[str, Any],
) -> None:
    await run_sentence_extraction_worker(db)


async def _handle_extract_claims(
    db: Database,
    job: Dict[str, Any],
) -> None:
    await run_claim_extraction_worker(db)


# -------------------------------------------------
# Register handlers
# -------------------------------------------------

JOB_HANDLERS.update({
    "dag_to_document_section": _handle_dag_to_document_section,
    "extract_sentences": _handle_extract_sentences,
    "extract_claims": _handle_extract_claims,
})


# -------------------------------------------------
# Runner loop
# -------------------------------------------------

async def run_runner(poll_interval: float = 1.0) -> None:
    db = Database(settings.db_dsn)
    await db.connect()

    logger.info("Pipeline runner started (poll_interval=%s)", poll_interval)

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
                await mark_failed(
                    db,
                    job_id,
                    f"No handler registered for job_type={job_type!r}",
                )
                continue

            try:
                await mark_running(db, job_id)
                await handler(db, job)
                await mark_done(db, job_id)
            except Exception as exc:
                logger.exception(
                    "Job %s (%s) failed",
                    job_id,
                    job_type,
                )
                await mark_failed(db, job_id, repr(exc))

    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        run_runner(
            poll_interval=settings.runner_poll_interval
        )
    )           

