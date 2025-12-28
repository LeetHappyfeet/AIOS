# aios_app/pipeline/worker.py

from __future__ import annotations

import asyncio
import logging
import traceback
import inspect
from typing import Dict, Callable, Any

from ..db import Database
from ..config import settings
from .jobs import mark_running, mark_done, mark_failed, fetch_next_job

logger = logging.getLogger("aios.pipeline.worker")


# -------------------------------------------------
# Job handler registry
# -------------------------------------------------

def load_job_handlers() -> Dict[str, Callable[..., Any]]:
    """
    Register all pipeline job handlers here.

    Naming rule:
    - job_type string must match pipeline_job.job_type
    - handler signature: (db, payload) -> None | Awaitable[None]
    """

    from .sources.wiki import handle_fetch_wiki
    from ..extract.segments import handle_split_sections
    from ..extract.sentences import handle_split_sentences
    from ..extract.claims import handle_extract_claims

    # future:
    # from .embed.context_vectors import handle_embed_context_vectors
    # from .embed.claim_vectors import handle_embed_claim_vectors


    return {
        "fetch_wiki": handle_fetch_wiki,
        "split_sections": handle_split_sections,
        "split_sentences": handle_split_sentences,
        "extract_claims": handle_extract_claims,
        # "embed_context_vectors": handle_embed_context_vectors,
        # "embed_claim_vectors": handle_embed_claim_vectors,
    }


# -------------------------------------------------
# Worker loop
# -------------------------------------------------

async def run_worker(poll_interval: float = 1.0) -> None:
    db = Database(settings.db_dsn)
    await db.connect()

    handlers = load_job_handlers()

    logger.info("AIOS pipeline worker started with %d handlers", len(handlers))

    try:
        while True:
            job = await fetch_next_job(db)

            if not job:
                await asyncio.sleep(poll_interval)
                continue

            job_id = job["job_id"]
            job_type = job["job_type"]
            payload = job["payload"]

            handler = handlers.get(job_type)

            if not handler:
                logger.error(
                    "No handler registered for job_type=%s payload=%s",
                    job_type,
                    payload,
                )
                await mark_failed(
                    db,
                    job_id,
                    f"No handler for job_type={job_type}",
                )
                continue

            try:
                await mark_running(db, job_id)
                logger.info(
                    "Running job %s (%s) payload=%s",
                    job_id,
                    job_type,
                    payload,
                )

                # 🔑 Support both sync and async handlers
                result = handler(db, payload)
                if inspect.isawaitable(result):
                    await result

                await mark_done(db, job_id)
                logger.info("Completed job %s (%s)", job_id, job_type)

            except Exception as e:
                logger.error(
                    "Job %s (%s) failed: %s\n%s",
                    job_id,
                    job_type,
                    e,
                    traceback.format_exc(),
                )
                await mark_failed(db, job_id, str(e))

    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
