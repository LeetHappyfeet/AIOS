from aios_app.ingest_identity import (
    IngestEventDisposition,
    classify_ingest_event,
    should_short_circuit_replay,
)


def test_new_event_is_classified_as_new():
    assert classify_ingest_event(inserted=True, was_superseded=False) is IngestEventDisposition.NEW


def test_existing_active_event_is_idempotent_replay():
    assert (
        classify_ingest_event(inserted=False, was_superseded=False)
        is IngestEventDisposition.ACTIVE_REPLAY
    )


def test_existing_superseded_event_is_reselection_not_replay():
    assert (
        classify_ingest_event(inserted=False, was_superseded=True)
        is IngestEventDisposition.SUPERSEDED_RESELECTION
    )


def test_inserted_event_wins_over_superseded_flag():
    assert (
        classify_ingest_event(inserted=True, was_superseded=True)
        is IngestEventDisposition.NEW
    )


def test_durable_active_replay_short_circuits():
    assert should_short_circuit_replay(
        IngestEventDisposition.ACTIVE_REPLAY,
        has_dag_node=True,
    )


def test_partial_active_replay_does_not_short_circuit():
    assert not should_short_circuit_replay(
        IngestEventDisposition.ACTIVE_REPLAY,
        has_dag_node=False,
    )


def test_new_and_reselected_events_never_short_circuit():
    assert not should_short_circuit_replay(
        IngestEventDisposition.NEW,
        has_dag_node=True,
    )
    assert not should_short_circuit_replay(
        IngestEventDisposition.SUPERSEDED_RESELECTION,
        has_dag_node=True,
    )
