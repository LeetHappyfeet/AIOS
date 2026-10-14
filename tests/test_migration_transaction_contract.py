"""Canonical post-baseline chain must be immutable and transactionally receipted."""
import pytest

from aios_app.migrate import (
    BASELINE_RECEIPT, REQUIRED_ACTIVE_MIGRATIONS, _migration_body,
)


def test_explicit_sql_wrapper_is_replaced_by_python_transaction():
    source = "-- explanatory header\nBEGIN;\nCREATE TABLE aios.fixture(id integer);\nCOMMIT;\n-- end\n"
    unwrapped = _migration_body(source, "fixture.sql")
    assert "CREATE TABLE aios.fixture" in unwrapped
    assert "BEGIN;" not in unwrapped
    assert "COMMIT;" not in unwrapped


def test_unwrapped_sql_remains_valid():
    source = "CREATE TABLE aios.fixture(id integer);"
    assert _migration_body(source, "fixture.sql") == source


@pytest.mark.parametrize("source", [
    "BEGIN; CREATE TABLE aios.fixture(id integer);",
    "CREATE TABLE aios.fixture(id integer); COMMIT;",
])
def test_partial_outer_transaction_fails_before_database_mutation(source):
    with pytest.raises(RuntimeError, match="Unbalanced outer transaction"):
        _migration_body(source, "broken.sql")


def test_canonical_receipts_are_required_and_baseline_is_not_rewritten():
    assert BASELINE_RECEIPT == "0001_aios_baseline"
    assert "20261003_10_integrated_belief_policy_and_integrity.sql" in REQUIRED_ACTIVE_MIGRATIONS
    assert "20261003_14_participation_worker_heartbeat.sql" in REQUIRED_ACTIVE_MIGRATIONS
