"""Keep incremental migration files append-only in canonical sort order."""
from pathlib import Path

from aios_app import migrate


def test_semantic_convergence_migration_follows_existing_november_8_receipts():
    names = sorted(p.name for p in migrate.MIGRATIONS_DIR.glob("*.sql"))
    previous = (
        "20261108_01_workspace_memory.sql",
        "20261108_02_place_reference_evidence.sql",
    )
    latest = "20261108_03_semantic_convergence_scan_indexes.sql"
    assert latest in migrate.REQUIRED_ACTIVE_MIGRATIONS
    assert all(name in names for name in (*previous, latest))
    assert max(names.index(name) for name in previous) < names.index(latest)
    assert "20261108_01_semantic_convergence_scan_indexes.sql" not in names
