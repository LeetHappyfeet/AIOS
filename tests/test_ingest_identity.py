from aios_app.ingest_identity import IngestEventDisposition, classify_ingest_event


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
    # Defensive invariant: a newly inserted row cannot meaningfully be a
    # historical re-selection even if a caller supplies inconsistent metadata.
    assert (
        classify_ingest_event(inserted=True, was_superseded=True)
        is IngestEventDisposition.NEW
    )
