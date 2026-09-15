from __future__ import annotations

from enum import Enum


class IngestEventDisposition(str, Enum):
    """Identity-level result of resolving an incoming ingest event.

    This deliberately describes event identity only. Runtime/DAG side effects
    remain the responsibility of the caller so replay short-circuiting can be
    introduced separately.
    """

    NEW = "new"
    ACTIVE_REPLAY = "active_replay"
    SUPERSEDED_RESELECTION = "superseded_reselection"


def classify_ingest_event(*, inserted: bool, was_superseded: bool) -> IngestEventDisposition:
    """Classify an ingest-event resolution without changing downstream behavior.

    NEW
        The dedupe identity did not previously exist.
    ACTIVE_REPLAY
        The exact immutable event already exists and is currently active. This
        is the case Part 2 may safely short-circuit before DAG/runtime work.
    SUPERSEDED_RESELECTION
        The exact immutable event exists but was superseded, e.g. a user swipes
        back to an earlier SillyTavern alternative. This must remain actionable
        and must not be treated as an idempotent no-op.
    """
    if inserted:
        return IngestEventDisposition.NEW
    if was_superseded:
        return IngestEventDisposition.SUPERSEDED_RESELECTION
    return IngestEventDisposition.ACTIVE_REPLAY
