"""Cooperative ownership guard for cancellable async pipeline handlers."""
from __future__ import annotations

import asyncio

from .jobs import JobDeferred


class LeaseLost(RuntimeError):
    pass


async def run_owned(work, heartbeat, *, timeout=60.0):
    """Watch the entire execution, including connection and lock acquisition.

    Use only with cancellable async handlers. Cancelling a to_thread wrapper
    cannot stop the thread and must not be treated as termination.
    """
    execution = asyncio.create_task(work)
    try:
        done, _ = await asyncio.wait({execution, heartbeat}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if execution in done:
            return await execution
        if heartbeat in done:
            cause = None if heartbeat.cancelled() else heartbeat.exception()
            raise LeaseLost("pipeline lease lost or heartbeat unavailable") from cause
        raise JobDeferred("lookup execution deadline elapsed", delay_seconds=60)
    finally:
        if not execution.done():
            execution.cancel()
        # The LOOKUP handler terminates its private DB pool on cancellation,
        # ensuring SQL cannot outlive this queue owner.
        await asyncio.gather(execution, return_exceptions=True)
