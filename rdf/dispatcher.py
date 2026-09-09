"""Retired compatibility entry point.

Pipeline work must execute through :mod:`aios_app.runner` so resource limits,
worker leases, semantic-scope locks, and the shared RDF gate are enforced.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("aios.rdf.dispatcher")


async def run_pending_jobs(db, limit: int = 10) -> None:
    """Reject the legacy direct-consumption path.

    The old dispatcher could claim arbitrary jobs and run handlers outside the
    bounded scheduler, bypassing the concurrency invariants introduced for safe
    parallel execution.
    """

    raise RuntimeError(
        "rdf.dispatcher.run_pending_jobs is retired; run aios_app.runner so "
        "pipeline leases, resource limits, scope locks, and RDF gating apply"
    )
