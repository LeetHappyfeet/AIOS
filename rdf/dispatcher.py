# aios_app/pipeline/dispatcher.py

from typing import Callable, Dict
import json
import logging

from .extract.segments import handle_split_sections
from .extract.sentences import handle_split_sentences
from .extract.claims import handle_extract_claims

logger = logging.getLogger("aios.pipeline.dispatcher")

JOB_HANDLERS: Dict[str, Callable] = {
    "split_sections": handle_split_sections,
    "split_sentences": handle_split_sentences,
    "extract_claims": handle_extract_claims,
    # later:
    # "embed_context_vectors": handle_embed_context_vectors,
    # "embed_claim_vectors": handle_embed_claim_vectors,
}


async def run_pending_jobs(db, limit: int = 10) -> None:
    jobs = await db.fetch(
        """
        SELECT job_id, job_type, payload
        FROM aios.pipeline_job
        WHERE status = 'pending'
        ORDER BY created_at
        LIMIT $1
        """,
        limit,
    )

    for job in jobs:
        job_id = job["job_id"]
        job_type = job["job_type"]
        payload = job["payload"]

        handler = JOB_HANDLERS.get(job_type)
        if not handler:
            logger.error("No handler for job_type=%s", job_type)
            await db.execute(
                "UPDATE aios.pipeline_job SET status='error' WHERE job_id=$1",
                job_id,
            )
            continue

        try:
            handler(db, payload)
            await db.execute(
                """
                UPDATE aios.pipeline_job
                SET status='done', finished_at=now()
                WHERE job_id=$1
                """,
                job_id,
            )
        except Exception as e:
            logger.exception("Job %s failed", job_id)
            await db.execute(
                """
                UPDATE aios.pipeline_job
                SET status='error', error=$2
                WHERE job_id=$1
                """,
                job_id, str(e),
            )