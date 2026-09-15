from __future__ import annotations

from enum import Enum


class IngestEventDisposition(str, Enum):
    """Identity-level result of resolving an incoming ingest event."""

    NEW = "new"
    ACTIVE_REPLAY = "active_replay"
    SUPERSEDED_RESELECTION = "superseded_reselection"


def classify_ingest_event(*, inserted: bool, was_superseded: bool) -> IngestEventDisposition:
    """Classify one resolved immutable ingest event."""
    if inserted:
        return IngestEventDisposition.NEW
    if was_superseded:
        return IngestEventDisposition.SUPERSEDED_RESELECTION
    return IngestEventDisposition.ACTIVE_REPLAY


def should_short_circuit_replay(
    disposition: IngestEventDisposition,
    *,
    has_dag_node: bool,
) -> bool:
    """Return true only when an exact active replay is already DAG-durable.

    A duplicate whose DAG node is not yet present is allowed to continue so a
    concurrent/partially completed first request can still finish structural
    ingestion. Superseded re-selections remain actionable for swipe handling.
    """
    return disposition is IngestEventDisposition.ACTIVE_REPLAY and has_dag_node
