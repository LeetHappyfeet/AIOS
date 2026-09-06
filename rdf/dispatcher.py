"""Compatibility shim for the retired RDF dispatcher.

The active ingestion pipeline is supervised by ``aios_app.supervisor`` and
executed by ``aios_app.runner``.  Those modules share the canonical
``queued -> running -> done/failed`` SQL job state machine implemented in
``pipeline.jobs``.

This module used to implement a second, incompatible queue protocol using
``pending``/``error`` states and columns that no longer exist.  Keeping that
implementation callable risks jobs being silently ignored or corrupting queue
state, so legacy callers are redirected to the canonical runner semantics.
"""

from __future__ import annotations

import logging

from aios_app.pipeline.jobs import fetch_next_job, mark_done, mark_failed
from aios_app.runner import JOB_HANDLERS

logger = logging.getLogger("aios.rdf.dispatcher")


async def run_pending_jobs(db, limit: int = 10) -> None:
    """Run up to ``limit`` jobs using the canonical pipeline state machine.

    New deployments should run ``aios_app.runner`` directly.  This function is
    retained only for backward compatibility with code that imported the old
    dispatcher entry point.
    """

    for _ in range(max(0, limit)):
        job = await fetch_next_job(db)
        if not job:
            return

        job_id = job["job_id"]
        job_type = job["job_type"]
        handler = JOB_HANDLERS.get(job_type)

        if not handler:
            error = f"No handler for job_type={job_type}"
            logger.error(error)
            await mark_failed(db, job_id, error)
            continue

        try:
            await handler(db, job)
            await mark_done(db, job_id)
        except Exception as exc:
            logger.exception("Job %s (%s) failed", job_id, job_type)
            await mark_failed(db, job_id, repr(exc))
