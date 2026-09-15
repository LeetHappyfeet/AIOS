from aios_app.ingest_identity import (
    IngestEventDisposition,
    should_short_circuit_replay,
)


def test_active_replay_with_dag_node_short_circuits():
    assert should_short_circuit_replay(
        IngestEventDisposition.ACTIVE_REPLAY,
        has_dag_node=True,
    )


def test_active_replay_without_dag_node_recovers_structural_ingest():
    assert not should_short_circuit_replay(
        IngestEventDisposition.ACTIVE_REPLAY,
        has_dag_node=False,
    )


def test_superseded_reselection_never_short_circuits():
    assert not should_short_circuit_replay(
        IngestEventDisposition.SUPERSEDED_RESELECTION,
        has_dag_node=True,
    )


def test_new_event_never_short_circuits():
    assert not should_short_circuit_replay(
        IngestEventDisposition.NEW,
        has_dag_node=True,
    )
